# -*- coding: utf-8 -*-
"""
接单监听器
------------------------------------------------
常驻后台，盯着剪贴板。你在微信里复制一条家教单，
旁边的页面会自动跳出匹配结果，不用再粘贴。

可选：配了 AI Key 会补全单子里缺的字段，
配了高德 Key 会按真实路线算通勤时间。
两个都不配也能正常用。

运行方式：双击「启动监听.bat」，或 python clip_watch.py
退出：在这个窗口按 Ctrl + C
"""

import ctypes
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
POLL_INTERVAL = 0.4

# config.json 读失败时的原因，启动横幅会打出来。
# 用户手写 JSON 少个逗号很常见，静默回退成空配置会让人以为 key 根本没生效。
config_problem = {"msg": ""}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------- 剪贴板 ----------------

IS_WIN = sys.platform.startswith("win")
CF_UNICODETEXT = 13

if IS_WIN:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
else:
    # 非 Windows 只用于跑单元测试，剪贴板功能不可用
    user32 = None
    kernel32 = None


def get_clipboard():
    """读剪贴板文本，读不到返回 None。"""
    if not IS_WIN:
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# ---------------- 判断像不像家教单 ----------------

STRONG = [
    "老师", "家教", "辅导", "补习", "一对一",
    "年级", "小学", "初中", "高中",
    "初一", "初二", "初三", "高一", "高二", "高三",
    "数学", "语文", "英语", "物理", "化学", "生物", "地理", "历史", "政治",
    "全科", "奥数", "钢琴", "美术", "编程",
    "课时", "每小时", "元/小时", "一小时", "每次", "每周",
]
MONEY = ["元", "块", "课时费", "时薪"]


def looks_like_order(text):
    """两条以上特征词，且带价格线索，才认为是家教需求。"""
    if not text:
        return False
    text = text.strip()
    if len(text) < 10 or len(text) > 4000:
        return False
    if text.startswith("http://") or text.startswith("https://"):
        return False

    hits = 0
    for kw in STRONG:
        if kw in text:
            hits += 1
            if hits >= 3:
                break
    if hits < 2:
        return False
    return any(m in text for m in MONEY)


# ---------------- AI 补全（可选，没配就用纯规则） ----------------

AI_PROMPT = """你是家教需求解析器。从用户给出的一条微信群消息里提取家教需求，只输出 JSON。

字段说明：
- grade: 年级，如 "初二"、"高一"、"小学三年级"。没提到填 null
- subjects: 科目数组，如 ["数学"]。没提到填 []
- freq: 每周次数，数字。没提到填 null
- duration: 每次课的小时数，数字。没提到填 null
- rate: 每小时课时费，数字。如果只给了单次总价，用总价除以时长换算。都没提到填 null
- isOnline: 是否线上课，布尔值
- location: 地点，如 "海淀"、"地铁4号线人民大学站"、"线上"。没提到填 null
- days: 星期数组，0 是周日，1 到 6 是周一到周六。"周末" 填 [6, 0]。没提到填 []
- timeOfDay: "上午" 或 "下午" 或 "晚上"，没有填 null
- teacherGender: 要求老师的性别，"男" 或 "女"，没有填 null
- settle: 结算方式，如 "月结"，没有填 null
- extra: 其他值得注意的信息，比如家长的要求、孩子的情况、语气里的暗示，不超过 40 字，没有填 null

注意：推断要克制，消息里没说的就不要编。只输出 JSON，不要任何解释文字。"""


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def load_config():
    cfg = {"base_url": "", "api_key": "", "model": "", "timeout": 20, "amap_key": ""}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
            config_problem["msg"] = ""
        else:
            config_problem["msg"] = "最外层应该是 { } 包起来的一组配置，现在拿到的是 %s" % type(data).__name__
    except FileNotFoundError:
        config_problem["msg"] = ""
    except Exception as exc:
        # 手写 JSON 少个逗号、多一个引号都到这里。静默回退会让用户以为 key 没用。
        config_problem["msg"] = str(exc)[:160]
    for key, env in (
        ("base_url", "AI_BASE_URL"),
        ("api_key", "AI_API_KEY"),
        ("model", "AI_MODEL"),
        ("amap_key", "AMAP_KEY"),
    ):
        val = os.environ.get(env) or cfg.get(key) or ""
        cfg[key] = str(val).strip()
    return cfg


def ai_enabled():
    cfg = load_config()
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


def extract_json(s):
    if not s:
        raise ValueError("模型返回空内容")
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        s = m.group(0)
    return json.loads(s)


def _ai_json(system_prompt, user_text, max_tokens):
    """调一次模型，返回 (dict, None) 或 (None, 错误信息)。

    补字段和拆单共用：都先带 response_format 走一遍，接口不认再退回普通模式。
    """
    cfg = load_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return None, "没有配置 AI"

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"

    def send(body):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + cfg["api_key"],
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout") or 20), context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        try:
            data = send(dict(payload, response_format={"type": "json_object"}))
        except Exception:
            data = send(payload)
        content = data["choices"][0]["message"]["content"]
        return extract_json(content), None
    except Exception as exc:
        return None, str(exc)[:220]


def call_ai(text):
    """返回 (dict, None) 或 (None, 错误信息)。"""
    return _ai_json(AI_PROMPT, text[:3000], 600)


# 拆单专用提示词。只让模型报「哪一行是开头」，不让它复述原文——
# 抄几千字很容易漏字改标点，后面的字段解析就跟着错。
SPLIT_PROMPT = """你要把一段家教群消息切成若干条彼此独立的单子。
我会按行给你文本，每行前面有行号和一个制表符。你只需要判断「哪一行是一条新单子的第一行」。

新单子的开头通常是这些形式：
- 编号行：#1051、【编号】13205、编号：GZ6102226、【单号】
- 机构代码：XMY-102201、ZX2130、DD2007
- 城市加日期码：广州BY26102206、佛山26102202C、深圳线下26050501
- 表情开头：🎃🎃佛山26102202C、🍀广州xxx
- 中介单号：家教宝贝粥粥: gz10221626
- 一排破折号或星号的分隔线，它下面第一行往往是新单子

判断时注意：
- 一条单子内部的多行（地址、年级、科目、时间、薪资、要求）都属于同一条，不要切开
- 同一条单子被转发两次，只算一条，第二份不要单独成条
- 如果某行确实像另一条单子的开始，就把它列进去
- 只有一整条内容时，就只输出 [1]

只输出 JSON：{"starts":[1,9,16]}
数字是行号，从 1 开始，必须递增，第一项必须是 1。不要输出别的任何字。"""

SPLIT_MAX_LINES = 600


def ai_split(text):
    """让模型判断每条单子从第几行开始，返回 (行号列表, None) 或 (None, 错误)。

    只报行号，原文由调用方自己切，保证切出来的内容和用户粘的一字不差。
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > SPLIT_MAX_LINES:
        return None, "文本超过 %d 行，先按规则切" % SPLIT_MAX_LINES
    numbered = "\n".join("%d\t%s" % (i + 1, l) for i, l in enumerate(lines))

    data, err = _ai_json(SPLIT_PROMPT, numbered, 900)
    if data is None:
        return None, err
    starts = data.get("starts")
    if not isinstance(starts, list) or not starts:
        return None, "AI 没按格式返回 starts"

    out = []
    for v in starts:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None, "AI 返回的行号不是数字"
        # 有一个越界就整体作废。只丢掉越界那个的话，末尾几条会被并进
        # 前一条，结果是悄悄少了几单，比直接不用 AI 还糟。
        if not (1 <= n <= len(lines)):
            return None, "AI 给的行号超出了文本范围"
        out.append(n)
    return out, None


# ---------------- 通勤时间（高德地图，可选） ----------------
#
# 只做两件事：把地名转成坐标，再按坐标算真实通勤时长。
# 不猜、不编，查不到就返回 None，前端退回手填的区域分钟数。

AMAP_BASE = "https://restapi.amap.com"
_geo_cache = {}
_route_cache = {}
_cache_lock = threading.Lock()


def amap_base():
    """允许用 AMAP_BASE_URL 覆盖接口地址，主要给测试用。"""
    return (os.environ.get("AMAP_BASE_URL") or AMAP_BASE).rstrip("/")


def amap_enabled():
    return bool(load_config().get("amap_key"))


def _amap(path, params):
    """调高德 Web 服务接口。高德失败时 HTTP 仍是 200，靠 status 字段判断。"""
    cfg = load_config()
    query = dict(params)
    query["key"] = cfg["amap_key"]
    query["output"] = "JSON"
    url = amap_base() + path + "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": "tutor-order-advisor/1.0"})
    with urllib.request.urlopen(req, timeout=8, context=_ssl_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if str(data.get("status")) != "1":
        raise RuntimeError(data.get("info") or "高德接口返回失败")
    return data


def lookup_place(text, city=""):
    """地名 → {location, city, adcode, name}，找不到返回 None。

    先走 POI 搜索（认「人大附中」「望京SOHO」这类具体地点），
    失败再走地理编码（认「海淀区中关村大街1号」这类地址）。
    """
    text = (text or "").strip()
    if not text:
        return None
    cache_key = (text, city)
    with _cache_lock:
        if cache_key in _geo_cache:
            return _geo_cache[cache_key]

    found = None
    try:
        params = {"keywords": text, "offset": 1, "page": 1}
        if city:
            params["city"] = city
            params["citylimit"] = "true"
        pois = _amap("/v3/place/text", params).get("pois") or []
        if pois and pois[0].get("location"):
            p = pois[0]
            found = {
                "location": p["location"],
                "city": p.get("cityname") or city,
                "adcode": str(p.get("adcode") or ""),
                "name": p.get("name") or text,
            }
    except Exception:
        pass

    if found is None:
        try:
            params = {"address": text}
            if city:
                params["city"] = city
            geos = _amap("/v3/geocode/geo", params).get("geocodes") or []
            if geos and geos[0].get("location"):
                g = geos[0]
                found = {
                    "location": g["location"],
                    "city": g.get("city") or city,
                    "adcode": str(g.get("adcode") or ""),
                    "name": g.get("formatted_address") or text,
                }
        except Exception:
            pass

    with _cache_lock:
        _geo_cache[cache_key] = found
    return found


def _seconds_to_minutes(seconds):
    try:
        m = int(round(float(seconds) / 60.0))
    except (TypeError, ValueError):
        return 0
    return m if m > 0 else 0


def _transit_extra(transit):
    """把公交方案里的线路名、换乘次数、步行距离抽出来。

    高德给的线路名带方向括号，比如「地铁3号线(番禺广场--天河客运站)」，
    去掉括号只留「地铁3号线」，够看就行。
    """
    lines = []
    walk_m = 0.0
    rides = 0
    for seg in (transit or {}).get("segments") or []:
        try:
            walk_m += float((seg.get("walking") or {}).get("distance") or 0)
        except (TypeError, ValueError):
            pass
        buslines = (seg.get("bus") or {}).get("buslines") or []
        if buslines:
            name = re.sub(r"[（(].*?[）)]", "", str(buslines[0].get("name") or "")).strip()
            if name:
                rides += 1
                lines.append(name)
    return {"lines": lines, "transfers": max(0, rides - 1), "walkMeters": int(round(walk_m))}


def route_between(origin_loc, dest_loc, city=""):
    """两个坐标之间的通勤时长，返回 (分钟, 方式, 距离米, 明细) 或 None。

    公交优先，步行和驾车兜底。家教单里同城短途居多，公交/地铁是最常见的通勤方式。
    明细里带线路名和换乘次数，页面上要显示「坐几号线、换几次」。
    """
    base = {"origin": origin_loc, "destination": dest_loc}

    try:
        q = dict(base, city=city, strategy=0, nightflag=0)
        route = _amap("/v3/direction/transit/integrated", q).get("route") or {}
        transits = route.get("transits") or []
        if transits:
            minutes = _seconds_to_minutes(transits[0].get("duration"))
            if minutes:
                return (
                    minutes,
                    "公交",
                    int(float(route.get("distance") or 0)),
                    _transit_extra(transits[0]),
                )
    except Exception:
        pass

    try:
        route = _amap("/v3/direction/walking", base).get("route") or {}
        paths = route.get("paths") or []
        if paths:
            minutes = _seconds_to_minutes(paths[0].get("duration"))
            if minutes:
                return minutes, "步行", int(float(paths[0].get("distance") or 0)), {}
    except Exception:
        pass

    try:
        route = _amap("/v3/direction/driving", base).get("route") or {}
        paths = route.get("paths") or []
        if paths:
            minutes = _seconds_to_minutes(paths[0].get("duration"))
            if minutes:
                return minutes, "驾车", int(float(paths[0].get("distance") or 0)), {}
    except Exception:
        pass

    return None


def commute(origin_text, dest_text, city=""):
    """返回 (结果 dict, None) 或 (None, 错误说明)。"""
    origin_text = (origin_text or "").strip()
    dest_text = (dest_text or "").strip()
    city = (city or "").strip()

    if not amap_enabled():
        return None, "没有配置高德 Key"
    if not origin_text:
        return None, "没有填出发点"
    if not dest_text:
        return None, "单子里没写地点"

    cache_key = (origin_text, dest_text, city)
    with _cache_lock:
        if cache_key in _route_cache:
            return _route_cache[cache_key], None

    try:
        origin = lookup_place(origin_text, city)
        if not origin:
            return None, "找不到出发点「%s」" % origin_text[:24]
        scope = city or origin.get("city") or ""
        dest = lookup_place(dest_text, scope)
        if not dest:
            return None, "找不到地点「%s」" % dest_text[:24]

        found = route_between(origin["location"], dest["location"], scope)
        if not found:
            return None, "没查到通勤方案"

        minutes, mode, meters, extra = found
        result = {
            "minutes": minutes,
            "mode": mode,
            "meters": meters,
            "lines": extra.get("lines") or [],
            "transfers": extra.get("transfers") or 0,
            "walkMeters": extra.get("walkMeters") or 0,
            "originName": origin.get("name") or origin_text,
            "destName": dest.get("name") or dest_text,
        }
    except Exception as exc:
        return None, "高德请求失败：%s" % str(exc)[:180]

    with _cache_lock:
        _route_cache[cache_key] = result
    return result, None


# ---------------- 共享状态 ----------------

state = {"seq": 0, "text": "", "ts": 0}
state_lock = threading.Lock()
last_seen = {"text": ""}


def watch_loop():
    while True:
        try:
            text = get_clipboard()
        except Exception:
            text = None

        if text and text != last_seen["text"] and looks_like_order(text):
            last_seen["text"] = text
            with state_lock:
                state["seq"] += 1
                state["text"] = text
                state["ts"] = time.time()
            preview = text.replace("\n", " ")[:36]
            print("  [%s] 收到单子  %s..." % (time.strftime("%H:%M:%S"), preview))
        elif text:
            last_seen["text"] = text

        time.sleep(POLL_INTERVAL)


# ---------------- 本地服务 ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TutorWatch/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/latest":
            with state_lock:
                payload = json.dumps(state, ensure_ascii=False)
            self._send(200, payload.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/ping":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return

        if path == "/ai-status":
            payload = json.dumps({"enabled": ai_enabled()}, ensure_ascii=False)
            self._send(200, payload.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/map-status":
            payload = json.dumps({"enabled": amap_enabled()}, ensure_ascii=False)
            self._send(200, payload.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    body = f.read()
                self._send(200, body, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "找不到 index.html".encode("utf-8"), "text/plain; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _read_json(self, limit=20000):
        """读请求体并解析 JSON，失败返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/parse":
            body = self._read_json()
            if body is None:
                self._json({"ok": False, "error": "json"}, 400)
                return
            text = (body.get("text") or "").strip()
            if not text:
                self._json({"ok": False, "error": "empty"}, 400)
                return
            data, err = call_ai(text)
            if data is None:
                self._json({"ok": False, "error": err})
            else:
                self._json({"ok": True, "data": data})
            return

        if path == "/split":
            body = self._read_json()
            if body is None:
                self._json({"ok": False, "error": "json"}, 400)
                return
            text = body.get("text") or ""
            if not text.strip():
                self._json({"ok": False, "error": "empty"}, 400)
                return
            starts, err = ai_split(text)
            if starts is None:
                self._json({"ok": False, "error": err})
            else:
                self._json({"ok": True, "starts": starts})
            return

        if path == "/commute":
            body = self._read_json()
            if body is None:
                self._json({"ok": False, "error": "json"}, 400)
                return
            result, err = commute(
                body.get("from") or "",
                body.get("to") or "",
                body.get("city") or "",
            )
            if result is None:
                self._json({"ok": False, "error": err})
            else:
                self._json({"ok": True, "data": result})
            return

        self._send(404, b'{"ok":false}', "application/json; charset=utf-8")


class LocalServer(ThreadingHTTPServer):
    """只监听本机，且排斥重复绑定。

    http.server 默认 allow_reuse_address=1，Windows 上 SO_REUSEADDR 的语义
    与 Unix 不同：它允许第二个进程绑定已被监听中的端口。表现是双击两次
    启动脚本，两个窗口都显示「已启动 · 8791」，请求却随机落到其中一个，
    /latest 的 seq 各算各的，页面就会时灵时不灵。这里改成独占。
    """

    allow_reuse_address = False
    daemon_threads = True


def main():
    if not IS_WIN:
        print("× 这个程序只能跑在 Windows 上（读剪贴板用的是 Win32 API）。")
        return

    if not os.path.exists(HTML_PATH):
        print("× 同目录下找不到 index.html，请把它和我放在一起。")
        input("按回车退出...")
        return

    server = None
    port = 0
    for candidate in range(8791, 8801):
        try:
            server = LocalServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue

    if server is None:
        print("× 8791-8800 端口都被占用了，先关掉别的程序再试。")
        input("按回车退出...")
        return

    print()
    print("  接单监听  已启动")
    print("  ------------------------------------")
    print("  地址   http://127.0.0.1:%d" % port)
    print("  用法   在微信里选中消息 → 右键复制")
    print("         页面会自动出匹配结果")
    cfg = load_config()
    if config_problem["msg"]:
        print("  配置  config.json 读不出来，已按「全空」处理")
        print("        原因：%s" % config_problem["msg"])
        print("        检查一下引号和逗号，或者直接删掉这个文件重来")
    if cfg["base_url"] and cfg["api_key"] and cfg["model"]:
        print("  AI     已开启（%s）· 缺失字段会自动补全" % cfg["model"])
    else:
        print("  AI     未配置 · 只用规则解析")
        print("         想开启就在同目录建 config.json，参考 config.example.json")
    if cfg["amap_key"]:
        print("  地图   高德已配置 · 按真实通勤时间算距离")
    else:
        print("  地图   未配置 · 距离按你手填的区域分钟数算")
    print("  退出   在这个窗口按 Ctrl + C")
    print()

    threading.Thread(target=watch_loop, daemon=True).start()
    if "--no-open" not in sys.argv:
        webbrowser.open("http://127.0.0.1:%d/" % port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
