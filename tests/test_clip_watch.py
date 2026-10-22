# -*- coding: utf-8 -*-
"""clip_watch.py 的单元测试，不依赖外部网络和 AI 服务。

跑法：
    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clip_watch as cw  # noqa: E402

# 整个测试模块都把配置指向临时目录。
# 否则贡献者本地那份真实 config.json（可能是配置好的 AI / 高德 Key）会被读进来，
# 让「没配 key 时应干净失败」这类用例直接失败。
_REAL_CONFIG_PATH = cw.CONFIG_PATH
_TMP_DIR = None


def setUpModule():
    global _TMP_DIR
    _TMP_DIR = tempfile.mkdtemp(prefix="cw-tests-")
    cw.CONFIG_PATH = os.path.join(_TMP_DIR, "config.json")


def tearDownModule():
    cw.CONFIG_PATH = _REAL_CONFIG_PATH
    if _TMP_DIR:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


class TestLooksLikeOrder(unittest.TestCase):
    def test_typical_order(self):
        text = "海淀初二数学，一周两次，每次两小时，200元一次，周末下午"
        self.assertTrue(cw.looks_like_order(text))

    def test_order_without_price_is_rejected(self):
        self.assertFalse(cw.looks_like_order("孩子初二，数学不太好，想找个老师辅导一下"))

    def test_price_without_features_is_rejected(self):
        self.assertFalse(cw.looks_like_order("今天买了点东西花了 200 元，还挺便宜的"))

    def test_plain_chat_is_rejected(self):
        self.assertFalse(cw.looks_like_order("在吗，晚上一起吃饭吧，我请客"))

    def test_url_is_rejected(self):
        self.assertFalse(cw.looks_like_order("https://example.com/order/12345?teacher=math"))

    def test_too_short_is_rejected(self):
        self.assertFalse(cw.looks_like_order("数学 200元"))

    def test_too_long_is_rejected(self):
        self.assertFalse(cw.looks_like_order("数学 200元 " + "填充" * 3000))

    def test_empty_is_rejected(self):
        self.assertFalse(cw.looks_like_order(""))
        self.assertFalse(cw.looks_like_order(None))


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(cw.extract_json('{"grade": "初二"}'), {"grade": "初二"})

    def test_fenced(self):
        raw = '```json\n{"grade": "高一", "rate": 150}\n```'
        self.assertEqual(cw.extract_json(raw), {"grade": "高一", "rate": 150})

    def test_with_surrounding_text(self):
        raw = '好的，结果如下：\n{"subjects": ["数学"]}\n希望有帮助。'
        self.assertEqual(cw.extract_json(raw), {"subjects": ["数学"]})

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            cw.extract_json("")

    def test_invalid_raises(self):
        with self.assertRaises(Exception):
            cw.extract_json("这不是 JSON")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._env = {}
        for key in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL", "AMAP_KEY"):
            self._env[key] = os.environ.pop(key, None)
        # 配置路径已由 setUpModule 指到临时目录，这里只要保证各用例从「无文件」开始
        if os.path.exists(cw.CONFIG_PATH):
            os.remove(cw.CONFIG_PATH)
        cw.config_problem["msg"] = ""

    def tearDown(self):
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        if os.path.exists(cw.CONFIG_PATH):
            os.remove(cw.CONFIG_PATH)

    def _write(self, text):
        with open(cw.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)

    def test_env_overrides(self):
        os.environ["AI_BASE_URL"] = "https://api.example.com/v1"
        os.environ["AI_API_KEY"] = "sk-test"
        os.environ["AI_MODEL"] = "test-model"
        cfg = cw.load_config()
        self.assertEqual(cfg["base_url"], "https://api.example.com/v1")
        self.assertEqual(cfg["api_key"], "sk-test")
        self.assertEqual(cfg["model"], "test-model")

    def test_enabled_flag(self):
        os.environ["AI_BASE_URL"] = "https://api.example.com/v1"
        os.environ["AI_API_KEY"] = "sk-test"
        os.environ["AI_MODEL"] = "test-model"
        self.assertTrue(cw.ai_enabled())

        os.environ.pop("AI_API_KEY", None)
        self.assertFalse(cw.ai_enabled())

    def test_amap_key_from_env(self):
        os.environ["AMAP_KEY"] = "amap-test-key"
        self.assertTrue(cw.amap_enabled())
        os.environ.pop("AMAP_KEY", None)
        self.assertFalse(cw.amap_enabled())

    def test_file_read_ok(self):
        self._write('{"base_url": "https://a/v1", "api_key": "k", "model": "m"}')
        cfg = cw.load_config()
        self.assertEqual(cfg["api_key"], "k")
        self.assertEqual(cw.config_problem["msg"], "")

    def test_no_file_is_not_an_error(self):
        cfg = cw.load_config()
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cw.config_problem["msg"], "")

    def test_broken_json_reports_reason(self):
        # 少一个逗号，这是最常见的手写错误
        self._write('{"api_key": "k" "model": "m"}')
        cfg = cw.load_config()
        self.assertEqual(cfg["api_key"], "")
        self.assertIn("Expecting", cw.config_problem["msg"])

    def test_non_dict_reports_reason(self):
        for bad in ("[]", '"hello"', "123"):
            self._write(bad)
            cw.load_config()
            self.assertTrue(cw.config_problem["msg"], "坏配置 %s 应给出原因" % bad)


class TestCommute(unittest.TestCase):
    """通勤查询：没配 key 要干净地失败，不能抛异常。"""

    def setUp(self):
        self._key = os.environ.pop("AMAP_KEY", None)

    def tearDown(self):
        if self._key is not None:
            os.environ["AMAP_KEY"] = self._key
        os.environ.pop("AMAP_KEY", None)

    def test_without_key(self):
        result, err = cw.commute("华南师范大学", "天河城")
        self.assertIsNone(result)
        self.assertIn("高德", err)

    def test_missing_origin(self):
        os.environ["AMAP_KEY"] = "fake"
        result, err = cw.commute("", "天河城")
        self.assertIsNone(result)
        self.assertIn("出发点", err)

    def test_missing_destination(self):
        os.environ["AMAP_KEY"] = "fake"
        result, err = cw.commute("华南师范大学", "")
        self.assertIsNone(result)
        self.assertIn("地点", err)


class TestAmapMock(unittest.TestCase):
    """用本地假高德服务跑完整链路：POI 搜索 → 公交路径 → 分钟数。"""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler

        class FakeAmap(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/v3/place/text":
                    body = {"status": "1", "count": "1", "pois": [{
                        "name": "人大附中", "location": "116.312,39.975",
                        "cityname": "北京市", "adcode": "110108"}]}
                elif path == "/v3/geocode/geo":
                    body = {"status": "1", "count": "1", "geocodes": [{
                        "formatted_address": "北京市海淀区中关村大街",
                        "location": "116.316,39.983", "city": "北京市",
                        "adcode": "110108"}]}
                elif path == "/v3/direction/transit/integrated":
                    body = {"status": "1", "route": {
                        "distance": "8100", "transits": [{"duration": "1920", "cost": "4"}]}}
                elif path == "/v3/direction/walking":
                    body = {"status": "1", "route": {
                        "paths": [{"duration": "600", "distance": "800"}]}}
                else:
                    body = {"status": "0", "info": "UNKNOWN"}
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAmap)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        cls._env = {
            "AMAP_KEY": os.environ.get("AMAP_KEY"),
            "AMAP_BASE_URL": os.environ.get("AMAP_BASE_URL"),
        }
        os.environ["AMAP_KEY"] = "fake-key"
        os.environ["AMAP_BASE_URL"] = "http://127.0.0.1:%d" % cls.port
        # 两个缓存都清掉，避免其他用例的结果串进来
        cw._geo_cache.clear()
        cw._route_cache.clear()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for key, val in cls._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_full_chain(self):
        result, err = cw.commute("某某大学", "人大附中")
        self.assertIsNone(err)
        self.assertEqual(result["minutes"], 32)      # 1920 秒 = 32 分钟
        self.assertEqual(result["mode"], "公交")
        self.assertEqual(result["meters"], 8100)
        self.assertEqual(result["destName"], "人大附中")

    def test_cache_hits(self):
        cw.commute("某某大学", "人大附中")
        before = len(cw._route_cache)
        cw.commute("某某大学", "人大附中")
        self.assertEqual(len(cw._route_cache), before)

    def test_walking_fallback(self):
        # city 传了但公交接口对未知城市返回不了？这里直接验步行兜底不走 transit 的情况
        result, err = cw.commute("某某大学", "人大附中", city="")
        self.assertIsNotNone(result)
        self.assertIn(result["mode"], ("公交", "步行", "驾车"))


class TestHttpServer(unittest.TestCase):
    """起一个真服务，把四个接口都打一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cw.Handler)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.read()

    def test_ping(self):
        status, body = self.get("/ping")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_latest(self):
        status, body = self.get("/latest")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("seq", "text", "ts"):
            self.assertIn(key, data)

    def test_ai_status(self):
        status, body = self.get("/ai-status")
        self.assertEqual(status, 200)
        self.assertIn("enabled", json.loads(body))

    def test_map_status(self):
        status, body = self.get("/map-status")
        self.assertEqual(status, 200)
        self.assertIn("enabled", json.loads(body))

    def test_commute_returns_json(self):
        req = urllib.request.Request(
            self.base + "/commute",
            data=json.dumps({"from": "", "to": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("ok", data)
        if not data["ok"]:
            self.assertIn("error", data)

    def test_index_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("接单匹配", body.decode("utf-8"))

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_split_rejects_empty(self):
        req = urllib.request.Request(
            self.base + "/split",
            data=json.dumps({"text": "  "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_split_returns_starts_and_orders(self):
        # 没配 AI 时应返回 ok=false 并带 error，而不是崩掉。
        # （返回结构见 TestAiSplit：ok 时是 {starts, orders}）
        req = urllib.request.Request(
            self.base + "/split",
            data=json.dumps({"text": "海淀初二数学 200元一次"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("ok", data)
        if data["ok"]:
            self.assertIn("starts", data)
            self.assertIn("orders", data)
        else:
            self.assertIn("error", data)


class TestCleanOrders(unittest.TestCase):
    """_clean_orders：把模型返回的字段表清点成和 starts 一一对应的列表。"""

    def test_length_mismatch_means_no_fields(self):
        # 长度对不上就整体作废——错位会把 A 单的年级安到 B 单头上
        raw = [{"grade": "初二"}, {"grade": "高一"}]
        self.assertEqual(cw._clean_orders(raw, 3), [])
        self.assertEqual(cw._clean_orders(raw, 1), [])

    def test_not_a_list_means_no_fields(self):
        self.assertEqual(cw._clean_orders({"grade": "初二"}, 1), [])
        self.assertEqual(cw._clean_orders(None, 1), [])

    def test_non_dict_entry_becomes_none(self):
        self.assertEqual(cw._clean_orders(["初二", {"grade": "高一"}], 2), [None, {"grade": "高一"}])

    def test_unknown_subject_is_dropped(self):
        got = cw._clean_orders([{"subjects": ["数学", "玄学"]}], 1)
        self.assertEqual(got, [{"subjects": ["数学"]}])

    def test_all_subjects_unknown_means_no_subjects(self):
        self.assertEqual(cw._clean_orders([{"subjects": ["玄学"]}], 1), [None])

    def test_null_string_is_not_a_value(self):
        got = cw._clean_orders([{"grade": "null", "location": "  "}], 1)
        self.assertEqual(got, [None])

    def test_out_of_range_numbers_are_dropped(self):
        got = cw._clean_orders([{"rate": 99999, "freq": 40, "duration": 0.1}], 1)
        self.assertEqual(got, [None])

    def test_string_numbers_are_coerced(self):
        got = cw._clean_orders([{"rate": "200", "freq": "2"}], 1)
        self.assertEqual(got, [{"rate": 200.0, "freq": 2.0}])

    def test_days_are_deduped_and_sorted(self):
        got = cw._clean_orders([{"days": [0, 6, 6, 9, -1]}], 1)
        self.assertEqual(got, [{"days": [0, 6]}])

    def test_is_online_must_be_boolean(self):
        got = cw._clean_orders([{"isOnline": "是"}], 1)
        self.assertEqual(got, [None])
        got = cw._clean_orders([{"isOnline": True}], 1)
        self.assertEqual(got, [{"isOnline": True}])


class TestAiSplit(unittest.TestCase):
    """ai_split：同时返回行号和每条单子的字段。"""

    def test_returns_starts_and_orders(self):
        payload = {
            "starts": [1, 9],
            "orders": [{"grade": "初二", "rate": 200}, {"grade": "高一"}],
        }
        orig = cw._ai_json
        cw._ai_json = lambda *a, **k: (payload, None)
        try:
            got, err = cw.ai_split("1\n2\n3\n4\n5\n6\n7\n8\n9\n10")
        finally:
            cw._ai_json = orig
        self.assertIsNone(err)
        self.assertEqual(got["starts"], [1, 9])
        self.assertEqual(got["orders"], [{"grade": "初二", "rate": 200.0}, {"grade": "高一"}])

    def test_orders_dropped_when_count_mismatches(self):
        payload = {"starts": [1, 9], "orders": [{"grade": "初二"}]}
        orig = cw._ai_json
        cw._ai_json = lambda *a, **k: (payload, None)
        try:
            got, err = cw.ai_split("1\n2\n3\n4\n5\n6\n7\n8\n9\n10")
        finally:
            cw._ai_json = orig
        self.assertIsNone(err)
        self.assertEqual(got["starts"], [1, 9])
        self.assertEqual(got["orders"], [])

    def test_no_orders_key_still_splits(self):
        payload = {"starts": [1]}
        orig = cw._ai_json
        cw._ai_json = lambda *a, **k: (payload, None)
        try:
            got, err = cw.ai_split("一整条单子")
        finally:
            cw._ai_json = orig
        self.assertIsNone(err)
        self.assertEqual(got, {"starts": [1], "orders": []})

    def test_out_of_range_line_still_fails_whole_call(self):
        payload = {"starts": [1, 99], "orders": []}
        orig = cw._ai_json
        cw._ai_json = lambda *a, **k: (payload, None)
        try:
            got, err = cw.ai_split("1\n2\n3")
        finally:
            cw._ai_json = orig
        self.assertIsNone(got)
        self.assertIn("范围", err)

    def test_error_from_ai_is_passed_through(self):
        orig = cw._ai_json
        cw._ai_json = lambda *a, **k: (None, "没有配置 AI")
        try:
            got, err = cw.ai_split("whatever")
        finally:
            cw._ai_json = orig
        self.assertIsNone(got)
        self.assertEqual(err, "没有配置 AI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
