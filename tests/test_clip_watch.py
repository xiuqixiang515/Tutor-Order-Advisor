# -*- coding: utf-8 -*-
"""clip_watch.py 的单元测试，不依赖外部网络和 AI 服务。

跑法：
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clip_watch as cw  # noqa: E402


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
        for key in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
            self._env[key] = os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

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

    def test_index_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("接单匹配", body.decode("utf-8"))

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_parse_rejects_empty(self):
        req = urllib.request.Request(
            self.base + "/parse",
            data=json.dumps({"text": "  "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_parse_returns_json(self):
        req = urllib.request.Request(
            self.base + "/parse",
            data=json.dumps({"text": "海淀初二数学 200元一次"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # 没配 AI 时应返回 ok=false 并带 error，而不是崩掉
        self.assertIn("ok", data)
        if not data["ok"]:
            self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
