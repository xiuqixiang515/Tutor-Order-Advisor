# -*- coding: utf-8 -*-
"""
接单监听器
------------------------------------------------
常驻后台，盯着剪贴板。你在微信里复制一条家教单，
旁边的页面会自动跳出匹配结果，不用再粘贴。

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
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
POLL_INTERVAL = 0.4

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
    cfg = {"base_url": "", "api_key": "", "model": "", "timeout": 20}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    for key, env in (("base_url", "AI_BASE_URL"), ("api_key", "AI_API_KEY"), ("model", "AI_MODEL")):
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


def call_ai(text):
    """返回 (dict, None) 或 (None, 错误信息)。"""
    cfg = load_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return None, "没有配置 AI"

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": AI_PROMPT},
            {"role": "user", "content": text[:3000]},
        ],
        "temperature": 0,
        "max_tokens": 600,
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

        if path in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    body = f.read()
                self._send(200, body, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "找不到 index.html".encode("utf-8"), "text/plain; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/parse":
            self._send(404, b'{"ok":false}', "application/json; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 20000:
            self._send(400, b'{"ok":false,"error":"length"}', "application/json; charset=utf-8")
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            text = (body.get("text") or "").strip()
        except Exception:
            self._send(400, b'{"ok":false,"error":"json"}', "application/json; charset=utf-8")
            return

        if not text:
            self._send(400, b'{"ok":false,"error":"empty"}', "application/json; charset=utf-8")
            return

        data, err = call_ai(text)
        if data is None:
            payload = json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        else:
            payload = json.dumps({"ok": True, "data": data}, ensure_ascii=False)
        self._send(200, payload.encode("utf-8"), "application/json; charset=utf-8")


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
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
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
    if ai_enabled():
        print("  AI     已开启（%s）· 缺失字段会自动补全" % cfg["model"])
    else:
        print("  AI     未配置 · 只用规则解析")
        print("         想开启就在同目录建 config.json，参考 config.example.json")
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
