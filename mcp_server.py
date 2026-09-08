#!/usr/bin/env python3
"""mcp_server.py — 把 tw-parking 包成一台 MCP server（stdio）。

**這是在使用者自己的機器上跑的本地程序**，不是誰架在網路上的服務：MCP client
（Claude Desktop、Claude Code、Cursor 之類）把它當子程序啟動，用標準輸入輸出
講 JSON-RPC。查詢直接從那台機器打向 parkboss 與各縣市政府端點，沒有中間人。

掛法（以 Claude Desktop 的設定檔為例）：

    {
      "mcpServers": {
        "tw-parking": {
          "command": "python3",
          "args": ["/絕對路徑/tw-parking/mcp_server.py"]
        }
      }
    }

沒有任何第三方套件依賴，Python 3.8 以上就能跑。

實作上刻意走子程序呼叫 `parking.py --json` 而不是 import 內部函式：那條路徑是
CLI 每天在用、驗過的同一條，包一層 MCP 不該讓它長出第二種行為。
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARKING = os.path.join(HERE, "parking.py")
PROTOCOL_FALLBACK = "2025-06-18"

TOOLS = [
    {
        "name": "find_parking",
        "description": (
            "查詢座標附近的停車場與即時剩餘車位。回傳每個場的名稱、距離、剩餘格數、"
            "總格數、資料時間、費率與導航連結。資料超過 24 小時的場不給車位數字。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "緯度，例如 25.047697"},
                "lon": {"type": "number", "description": "經度，例如 121.515114"},
                "radius": {"type": "number", "description": "搜尋半徑（公尺），預設 800",
                           "default": 800},
                "limit": {"type": "integer",
                          "description": "最多回傳幾個場，0 代表全部。預設 5",
                          "default": 5},
                "include_moto": {"type": "boolean",
                                 "description": "是否連機車專用場一起回傳，預設否",
                                 "default": False},
            },
            "required": ["lat", "lon"],
        },
    }
]


MAX_RADIUS_M = 20000.0
MAX_LIMIT = 100


def _num(args, key, default=None, lo=None, hi=None):
    """把參數轉成有限浮點數並夾在範圍內。

    NaN 與 inf 一律拒絕：距離比較 `d > radius` 對 NaN 恆為 False，
    半徑餵 NaN 會讓整個距離過濾靜默失效、把全台的場都吐出來
    （2026-09-08 codex 紅隊實測 nan_radius／inf_radius 都有回值）。
    """
    raw = args.get(key, default)
    if raw is None or raw == "":
        raw = default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 不是數字")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{key} 必須是有限數字")
    if lo is not None and v < lo:
        raise ValueError(f"{key} 超出範圍（{lo}〜{hi}）")
    if hi is not None and v > hi:
        raise ValueError(f"{key} 超出範圍（{lo}〜{hi}）")
    return v


def _find_parking(args):
    if not isinstance(args, dict):
        raise ValueError("arguments 必須是物件")
    lat = _num(args, "lat", lo=-90.0, hi=90.0)
    lon = _num(args, "lon", lo=-180.0, hi=180.0)
    radius = _num(args, "radius", default=800, lo=1.0, hi=MAX_RADIUS_M)
    limit = int(_num(args, "limit", default=5, lo=0, hi=MAX_LIMIT))

    cmd = [sys.executable, PARKING, "near", str(lat), str(lon),
           "--radius", str(radius), "--json"]
    if args.get("include_moto"):
        cmd.append("--include-moto")

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        # 子程序的 stderr 會帶內部路徑與堆疊，對未認證的呼叫者是一個免費的
        # 錯誤預言機（2026-09-08 codex 紅隊 #5）。細節只寫自己的 stderr。
        sys.stderr.write("parking.py rc=%s stderr=%s\n"
                         % (proc.returncode, proc.stderr.strip()[:2000]))
        raise RuntimeError("查詢上游失敗")

    records = json.loads(proc.stdout or "[]")
    if limit > 0:
        records = records[:limit]
    # parking.py 的 --json 自己就會給 navigation_url 與 availability_status
    # （2026-09-08 起）。這裡不再另外拼一個 nav_url——同一條網址掛兩個欄位名，
    # 下游會不知道該信哪一個。
    return records


def _handle(msg):
    """回傳要送出去的回應，或 None 代表這是通知不必回。"""
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": client_proto or PROTOCOL_FALLBACK,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tw-parking", "version": "1.0.0"},
        }}

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name != "find_parking":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"未知的工具: {name}"}}
        try:
            records = _find_parking(params.get("arguments") or {})
        except ValueError as e:
            # 參數錯誤照實說：那是呼叫端自己送的值，講清楚它才改得動，
            # 而且訊息內容全部由我們自己的 _num() 產生，不含內部細節。
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"查詢失敗: {e}"}],
                "isError": True}}
        except Exception as e:
            # 其餘一律固定字串：上游錯誤與堆疊不外流（codex 紅隊 #5）。
            # 工具層的失敗回成 isError 而不是 JSON-RPC error：讓模型自己決定
            # 要不要換個半徑重試，而不是整條連線被判成壞掉。
            sys.stderr.write(f"tool error: {type(e).__name__}: {e}\n")
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "查詢失敗: 上游暫時取不到資料"}],
                "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(records, ensure_ascii=False, indent=1)}],
            "isError": False}}

    if mid is None:
        return None   # 其餘通知一律不回
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未支援的方法: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
