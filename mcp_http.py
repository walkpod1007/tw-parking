#!/usr/bin/env python3
"""mcp_http.py — 把 tw-parking 的 MCP server 以 HTTP 對外提供（streamable HTTP）。

為什麼要有這支：`mcp_server.py` 走的是 stdio，只有「能在同一台機器上把它當
子行程啟動」的用戶端叫得動（Claude Code、Cursor 這類）。網頁版的 AI 連不到
你電腦上的本機服務，要接就得有一個網址——這支就是那個網址的本體。

安全面預設一道：`Authorization: Bearer <token>`。權杖從環境變數
`TW_PARKING_TOKEN` 讀，沒設就拒絕啟動（不給無密碼裸奔的預設值）。
**開放模式**：設 `TW_PARKING_OPEN=1` 才會放行不帶權杖的請求（2026-09-08 伊森
「到底要怎麼開放啊」——連 ChatGPT 這類自訂連接器時，少一格憑證就少一個卡點）。
要另外設一個環境變數而不是「沒權杖就自動開放」，是為了不讓忘記設權杖變成裸奔。
本服務吐的是各縣市停車場開放資料，沒有使用者資料，開放的代價是上游額度而不是隱私。
資料本身是政府公開停車資訊，權杖擋的是「別人拿我們的機器當免費代理」。

用法：
    TW_PARKING_TOKEN=xxxx python3 mcp_http.py --port 8901
"""

import argparse
import ipaddress
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_server  # noqa: E402  （同目錄，_handle 與工具定義都在那裡）

TOKEN = os.environ.get("TW_PARKING_TOKEN", "").strip()
OPEN_MODE = os.environ.get("TW_PARKING_OPEN", "").strip() in ("1", "true", "yes")
MAX_BODY = 256 * 1024

# 節流（2026-09-08 開放模式上線後補）：開放的代價是上游額度——苗栗／金門／屏東
# 的即時空位走 TDX，那條是有限額的。這裡不做帳號制，只做「同一個來源打太快就先擋」，
# 讓一個人打不爆全部的額度。數字寫成環境變數，要調不必改程式。
RATE_PER_MIN = int(os.environ.get("TW_PARKING_RATE_PER_MIN", "20") or 20)
GLOBAL_PER_MIN = int(os.environ.get("TW_PARKING_GLOBAL_PER_MIN", "120") or 120)
WINDOW = 60.0
MAX_KEY_LEN = 64        # 標頭可偽造：長度不設限＝拿記憶體換一行字
MAX_BUCKETS = 4096      # 分桶數硬上限，滿了整批重來（見 _rate_ok）
MAX_BATCH = 8           # JSON-RPC 批次筆數上限（一次 POST 只計一次費的放大面）

# 受信任代理（codex 紅隊 #4）：CF-Connecting-IP 是給我們的一份「來源是誰」的
# 說法，只有在**送這句話的人**確實是我們的反向代理時才值得採信。設成空字串
# ＝維持舊行為（誰說都信），設成 CIDR／IP 清單則只有對端落在清單內才讀標頭，
# 其餘一律以 socket 對端分桶——偽造標頭的人就只能偽造成自己。
TRUSTED_PROXIES = [x.strip() for x in
                   os.environ.get("TW_PARKING_TRUSTED_PROXIES", "").split(",")
                   if x.strip()]


def _peer_trusted(peer_ip):
    if not TRUSTED_PROXIES:
        return True          # 沒設定＝不啟用這道檢查
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for entry in TRUSTED_PROXIES:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False
_rl_lock = threading.Lock()
_rl_hits = {}          # client key -> deque[timestamp]
_rl_global = deque()   # 全站 timestamp


def _client_key(headers, fallback):
    if not _peer_trusted(fallback):
        # 對端不是我們認得的代理：它送什麼標頭都不算數，用 socket 對端分桶
        return str(fallback)[:MAX_KEY_LEN]
    # 前面隔著 Cloudflare，socket 對端是通道不是使用者；真正的來源在這個標頭。
    # 標頭可以偽造，但偽造者是在幫自己分桶，對「一個人吃光額度」這個威脅足夠。
    key = (headers.get("CF-Connecting-IP")
           or headers.get("X-Forwarded-For", "").split(",")[0].strip()
           or fallback)
    # 標頭是攻擊者寫的：不截長度就等於讓他用一行字換走我們的記憶體
    return key[:MAX_KEY_LEN]


def _rate_ok(key, cost=1):
    """回 (通過與否, 還要等幾秒)。兩道窗：全站先於單一來源。

    順序是刻意的（2026-09-08 codex 紅隊 #4）：舊版先對 key 做 setdefault
    才檢查全站，於是全站滿了之後每個假來源仍舊留下一個空 bucket，
    十萬個假標頭就是十萬個字典項目——被擋下的請求反而更省事地吃記憶體。
    現在全站先擋，擋下就直接返回，不碰 key 的字典。
    """
    now = time.time()
    with _rl_lock:
        while _rl_global and now - _rl_global[0] > WINDOW:
            _rl_global.popleft()
        if len(_rl_global) + cost > GLOBAL_PER_MIN:
            return False, max(1, int(WINDOW - (now - _rl_global[0])) + 1)

        bucket = _rl_hits.get(key)
        if bucket is None:
            if len(_rl_hits) >= MAX_BUCKETS:
                # 先掃空 bucket；掃不出空間就整批丟掉重來（節流退回全站窗那道，
                # 不會變成不設防）——寧可粗暴也不要無上限成長。
                for k in [k for k, v in _rl_hits.items() if not v]:
                    del _rl_hits[k]
                if len(_rl_hits) >= MAX_BUCKETS:
                    _rl_hits.clear()
            bucket = _rl_hits.setdefault(key, deque())
        while bucket and now - bucket[0] > WINDOW:
            bucket.popleft()
        if len(bucket) + cost > RATE_PER_MIN:
            return False, max(1, int(WINDOW - (now - bucket[0])) + 1)

        # 兩道都過才記帳，免得被擋下的請求還把自己算進另一道窗
        for _ in range(cost):
            bucket.append(now)
            _rl_global.append(now)
    return True, 0


class Handler(BaseHTTPRequestHandler):
    # 沒有 deadline 的 rfile.read() 加上不設限的執行緒模型＝慢速 body 就能
    # 把執行緒一條一條占住（codex 紅隊 #2）。20 秒對正常客戶端綽綽有餘。
    timeout = 20
    server_version = "tw-parking-mcp/1.0"

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if OPEN_MODE:
            return True
        got = self.headers.get("Authorization", "")
        # 常數時間比較不是重點（權杖不是密碼雜湊），但不要在錯的時候回傳細節
        return got == f"Bearer {TOKEN}"

    def do_GET(self):
        # 健康檢查刻意不要權杖：外面要能量得到「這個網址活著」，
        # 而它不吐任何資料。其餘路徑一律 404，不要洩漏有什麼。
        if self.path.rstrip("/").endswith("/health"):
            return self._send(200, {"ok": True, "service": "tw-parking-mcp"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        ok, retry = _rate_ok(_client_key(self.headers, self.client_address[0]))
        if not ok:
            self.send_response(429)
            body = json.dumps({"error": "rate limited", "retry_after": retry}).encode()
            self.send_header("Retry-After", str(retry))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad content-length"})
        if length <= 0 or length > MAX_BODY:
            return self._send(400, {"error": "bad body size"})
        raw = self.rfile.read(length)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse error"}})

        if not isinstance(msg, (dict, list)):
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": "invalid request"}})

        # JSON-RPC 批次也收：規格允許陣列，用戶端實作不一。
        # 但筆數要有上限、而且每一筆都要計費——否則 256 KiB 的 body 可以塞
        # 近三千個 find_parking，卻只在入口扣掉一格額度（codex 紅隊 #2／#3）。
        if isinstance(msg, list):
            if len(msg) > MAX_BATCH:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600,
                    "message": f"批次上限 {MAX_BATCH} 筆"}})
            extra = len(msg) - 1
            if extra > 0:
                ok2, retry2 = _rate_ok(
                    _client_key(self.headers, self.client_address[0]), cost=extra)
                if not ok2:
                    return self._send(429, {"error": "rate limited",
                                            "retry_after": retry2})
        msgs = msg if isinstance(msg, list) else [msg]
        out = []
        for m in msgs:
            if not isinstance(m, dict):
                # 非物件成員：舊版會讓錯誤處理自己再 .get() 一次而炸掉
                out.append({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": "invalid request"}})
                continue
            try:
                resp = mcp_server._handle(m)
            except Exception as e:  # 不讓單一訊息炸掉整條連線
                # 對外只給固定字串：原始 exception 文字對未認證的人是一個
                # 免費的錯誤預言機（codex 紅隊 #5）。細節只寫 stderr。
                sys.stderr.write(f"handler error: {type(e).__name__}: {e}\n")
                resp = {"jsonrpc": "2.0", "id": m.get("id"),
                        "error": {"code": -32603, "message": "internal error"}}
            if resp is not None:
                out.append(resp)

        if not out:
            # 全是通知（例如 notifications/initialized）→ 照規格回 202 空回應
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._send(200, out if isinstance(msg, list) else out[0])

    def log_message(self, fmt, *args):
        # 預設會把每一筆請求印到 stderr 且帶反解主機名（慢）。這裡自己寫一行，
        # 不記查詢內容——座標是使用者位置，不該落在伺服器日誌裡。
        # 路徑也要遮（codex 紅隊 #5）：對外那條網址的亂碼段本身就是門票，
        # 日誌若被集中收走就等於把門票一起送出去。只留前六個字元認得出是哪條。
        path = self.path or ""
        if len(path) > 8:
            path = path[:7] + "…"
        sys.stderr.write("%s %s %s peer=%s\n" % (self.log_date_time_string(),
                                                 self.command, path,
                                                 self.client_address[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    if not TOKEN and not OPEN_MODE:
        sys.stderr.write("FAIL 沒有設 TW_PARKING_TOKEN，拒絕以無認證方式啟動\n")
        sys.exit(2)
    if OPEN_MODE:
        sys.stderr.write("WARN TW_PARKING_OPEN=1：本次以開放模式啟動，POST 不驗權杖\n")
    sys.stderr.write(f"節流：單一來源 {RATE_PER_MIN}/分、全站 {GLOBAL_PER_MIN}/分\n")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"tw-parking MCP HTTP listening on {args.host}:{args.port}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
