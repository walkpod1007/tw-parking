#!/usr/bin/env bash
# ==============================================================================
# tests/parity.sh — 比對 tw-parking 基準端點與受測端點之行為一致性
#
# 比對規則：
# 1. tools/list：兩邊回傳之 JSON 結構與內容完全相同。
# 2. 六題查詢（台北101、板橋車站、台中車站、高雄巨蛋、台南火車站、花蓮火車站）：
#    - 每題以 find_parking(radius=800, limit=0) 查詢。
#    - 兩邊回傳的停車場「名稱集合」交集 >= 兩邊各自名稱集合大小的 80%
#      （空對空視為 PASS；即時空位數字因時間漂移不比對）。
#    - 相同場站之欄位名集合必須完全一致。
#    - 新鮮度欄位值域只允許 Python 版出現過之合法字串：
#      fresh_level 需為 live / recent / expired / never；
#      fresh_text / freshness 需為「即時」、「N 小時前」、「數字過期（最後更新 ...）」或「無即時回報」。
# 3. 驗證全數通過時印出 PARITY PASS 並以 exit 0 收尾；
#    任何不符合處印出 PARITY FAIL <原因> 並以 exit 1 收尾。
# ==============================================================================

set -eo pipefail

BASE1="${1:-https://nas.life-os.work/parking-0ff2dffdbc4dd154}"
BASE2="${2:-http://127.0.0.1:8787}"

python3 - "$BASE1" "$BASE2" << 'PYEOF'
import sys
import json
import urllib.request
import urllib.error
import re

base1 = sys.argv[1].rstrip("/")
base2 = sys.argv[2].rstrip("/")

def rpc_call(base_url, payload, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "ParityTest/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {base_url}: {body}")
    except Exception as e:
        raise RuntimeError(f"Request failed to {base_url}: {e}")

# 1. Check tools/list
try:
    t1 = rpc_call(base1, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    t2 = rpc_call(base2, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
except Exception as e:
    print(f"PARITY FAIL tools/list request failed: {e}")
    sys.exit(1)

tools1 = (t1.get("result") or {}).get("tools")
tools2 = (t2.get("result") or {}).get("tools")
if json.dumps(tools1, sort_keys=True) != json.dumps(tools2, sort_keys=True):
    print("PARITY FAIL tools/list mismatch between baseline and target")
    sys.exit(1)

# 2. Check 6 query locations
QUERIES = [
    ("Taipei 101", 25.033976, 121.564472),
    ("Banqiao Station", 25.013, 121.463),
    ("Taichung Station", 24.1369, 120.6866),
    ("Kaohsiung Arena", 22.6698, 120.3020),
    ("Tainan Station", 22.9971, 120.2127),
    ("Hualien Station", 23.9930, 121.6011),
]

ALLOWED_FRESH_LEVELS = {"live", "recent", "expired", "never"}

def validate_freshness(rec, loc_name):
    level = rec.get("fresh_level")
    if level not in ALLOWED_FRESH_LEVELS:
        return f"invalid fresh_level '{level}' in record {rec.get('name')}"
    text = str(rec.get("fresh_text") or "")
    if text == "即時":
        pass
    elif re.match(r"^\d+\s*小時前$", text):
        pass
    elif text.startswith("數字過期（最後更新"):
        pass
    elif text == "無即時回報":
        pass
    else:
        return f"invalid fresh_text '{text}' in record {rec.get('name')}"
    return None

for qname, lat, lon in QUERIES:
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "find_parking",
            "arguments": {"lat": lat, "lon": lon, "radius": 800, "limit": 0}
        }
    }
    try:
        r1 = rpc_call(base1, payload)
        r2 = rpc_call(base2, payload)
    except Exception as e:
        print(f"PARITY FAIL query '{qname}' failed: {e}")
        sys.exit(1)

    c1 = (r1.get("result") or {}).get("content", [{}])[0].get("text", "[]")
    c2 = (r2.get("result") or {}).get("content", [{}])[0].get("text", "[]")

    try:
        records1 = json.loads(c1)
        records2 = json.loads(c2)
    except Exception as e:
        print(f"PARITY FAIL query '{qname}' returned unparseable content: {e}")
        sys.exit(1)

    # Empty vs Empty is allowed (e.g. Hualien)
    if len(records1) == 0 and len(records2) == 0:
        continue

    # Name set intersection check
    names1 = set(r.get("name") for r in records1 if r.get("name"))
    names2 = set(r.get("name") for r in records2 if r.get("name"))
    common_names = names1 & names2

    if len(names1) > 0:
        ratio1 = len(common_names) / len(names1)
        if ratio1 < 0.8:
            print(f"PARITY FAIL {qname}: intersection {len(common_names)} is only {ratio1:.1%} of baseline ({len(names1)})")
            sys.exit(1)

    if len(names2) > 0:
        ratio2 = len(common_names) / len(names2)
        if ratio2 < 0.8:
            print(f"PARITY FAIL {qname}: intersection {len(common_names)} is only {ratio2:.1%} of target ({len(names2)})")
            sys.exit(1)

    # Validate freshness values in target records
    for r in records2:
        err = validate_freshness(r, qname)
        if err:
            print(f"PARITY FAIL {qname}: freshness validation failed: {err}")
            sys.exit(1)

    # Validate field names set matching for common lots
    map1 = {r["name"]: set(r.keys()) for r in records1 if "name" in r}
    map2 = {r["name"]: set(r.keys()) for r in records2 if "name" in r}
    for name in common_names:
        fields1 = map1[name]
        fields2 = map2[name]
        if fields1 != fields2:
            diff_missing = fields1 - fields2
            diff_extra = fields2 - fields1
            print(f"PARITY FAIL {qname}: field mismatch on '{name}': missing={diff_missing}, extra={diff_extra}")
            sys.exit(1)

print("PARITY PASS")
sys.exit(0)
PYEOF
