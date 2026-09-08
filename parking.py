#!/usr/bin/env python3
"""
即時停車位查詢 CLI 工具
資料源: parkboss.tw API + 10 個縣市政府自有停車端點
純 Python 標準函式庫實作
"""

import sys
import os
import json
import math
import re
import argparse
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
import sources


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """計算兩點經緯度間的大圓距離（單位：公尺）"""
    R = 6371000.0  # 地球平均半徑（公尺）
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def extract_rate_summary(pay_info: Optional[str]) -> str:
    """
    費率摘要：只取第一個「每小時收費N元」或「每半小時N元」片語，
    取不到就印前 30 字加刪節號。
    """
    if not pay_info or not isinstance(pay_info, str):
        return ""
    pay_info = pay_info.replace("\r", " ").replace("\n", " ").strip()

    # 費率寫法各縣市各家不同：「每小時30元」「每半小時收費40元」「100元/時」
    # 「40元/小時」都出現過。舊版只認前兩種，馬偕醫院那種 `計時：100元/時，…`
    # 比不到就退回「前 30 字加刪節號」，印出來是一整串沒斷句的條文，
    # 送 LINE 時每次都要人手改寫，格式就是這樣飄掉的。這裡把四種寫法收成同一個短句。
    HALF = [r"每半小時(?:收費)?(\d+)元", r"(\d+)元\s*/\s*半小時"]
    HOUR = [r"每小時(?:收費)?(\d+)元", r"(\d+)元\s*/\s*(?:小)?時"]
    for pat in HALF:
        m = re.search(pat, pay_info)
        if m:
            return f"每半小時 {m.group(1)} 元"
    for pat in HOUR:
        m = re.search(pat, pay_info)
        if m:
            return f"每小時 {m.group(1)} 元"
    # 還是比不到就給短句，不要把整段條文倒進來——長度上限比照一行讀得完。
    if len(pay_info) > 16:
        return pay_info[:16] + "…"
    return pay_info


TAIPEI = timezone(timedelta(hours=8))


def parse_car_time(time_str: Optional[str]) -> Optional[datetime]:
    """解析 car_time 字串為 datetime 物件"""
    if not time_str or not isinstance(time_str, str):
        return None
    time_str = time_str.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            pass
    # TDX 回的是帶時區的 ISO8601（`2026-09-08T12:01:20+08:00`），上面三個格式都比不到，
    # 於是剛剛才更新的即時值會被判成「數字過期」——比沒有數字更糟，因為它看起來像
    # 我們查過了而且過期了。這裡轉成本地時間的 naive datetime 與其他來源同尺。
    try:
        dt = datetime.fromisoformat(time_str)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        # agy 紅隊 MED：原本寫 `dt.astimezone()`＝轉成**本機時區**。排程與容器的
        # TZ 未必是 Asia/Taipei，在 UTC 環境下 TDX 的 +08:00 會被轉成 UTC naive，
        # 而其他縣府來源的 naive 時間本來就是台北時間——兩邊差 8 小時，
        # 剛更新的即時值會被判成 8 小時前。台灣的資料源一律釘台北，不吃本機 TZ。
        dt = dt.astimezone(TAIPEI).replace(tzinfo=None)
    return dt


def calculate_freshness(car_time_str: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    新鮮度分級：
    - 30 分鐘內 → 標「即時」 (fresh_level: "live")
    - 30 分鐘到 24 小時 → 標「N 小時前」 (fresh_level: "recent")
    - 超過 24 小時 → 不顯示車位數字，改標「數字過期（最後更新 YYYY-MM-DD）」 (fresh_level: "expired")
    """
    if now is None:
        now = datetime.now()

    car_dt = parse_car_time(car_time_str)
    if car_dt is None:
        # 「沒有時間戳」有兩種成因，講法要分開：
        #   a) 這個來源根本不提供即時空位（縣府的靜態場地資料）→「無即時回報」
        #   b) 有時間戳但格式沒認出來 → 照舊講「數字過期」並把原字串前 10 碼帶出來
        # 兩者都不顯示車位數字，但說法不能混——說「數字過期」等於宣稱它曾經回報過。
        if not car_time_str:
            return {
                "fresh_level": "never",
                "fresh_text": "無即時回報",
                "is_expired": True
            }
        return {
            "fresh_level": "expired",
            "fresh_text": f"數字過期（最後更新 {car_time_str[:10]}）",
            "is_expired": True
        }

    diff_seconds = (now - car_dt).total_seconds()
    if diff_seconds <= 1800:
        return {
            "fresh_level": "live",
            "fresh_text": "即時",
            "is_expired": False
        }
    elif diff_seconds <= 86400:
        hours = max(1, int(diff_seconds // 3600))
        return {
            "fresh_level": "recent",
            "fresh_text": f"{hours} 小時前",
            "is_expired": False
        }
    else:
        date_str = car_dt.strftime("%Y-%m-%d")
        return {
            "fresh_level": "expired",
            "fresh_text": f"數字過期（最後更新 {date_str}）",
            "is_expired": True
        }


def fetch_parking_spaces(lat: float, lon: float) -> List[Dict[str, Any]]:
    """向 parkboss.tw API 查詢座標半徑範圍內的停車位資料"""
    url = f"https://parkboss.tw/api/v1/query-parking-space-by-coordinate?lat={lat}&lon={lon}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                sys.stderr.write(f"HTTP error: {resp.status} {resp.reason}\n")
                sys.exit(1)
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP error: {e.code} {e.reason}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"Network error: {e}\n")
        sys.exit(1)

    try:
        data = json.loads(raw)
    except Exception as e:
        sys.stderr.write(f"JSON parse error: {e}\n")
        sys.exit(1)

    if not isinstance(data, list):
        sys.stderr.write(f"Unexpected response format: expected list, got {type(data).__name__}\n")
        sys.exit(1)

    for item in data:
        if "source" not in item:
            item["source"] = "parkboss"

    return data


def is_same_parking_lot(rec1: Dict[str, Any], rec2: Dict[str, Any]) -> bool:
    """去重判定：座標距離 < 50 公尺且名稱去掉空白後有一方包含另一方"""
    # 同一個來源內部**絕不去重**：parkboss 在南投等地大量回傳單格地磁，
    # 名稱一模一樣（「中山街」「復興路」）且彼此相距不到 50 公尺，
    # 用「名稱互相包含」的規則會把 679 筆吃到剩 109 筆。
    # 去重的目的只是消掉「同一個場同時出現在 parkboss 與縣府端點」那種跨來源重疊。
    if rec1.get("source") == rec2.get("source"):
        return False

    lat1 = rec1.get("lat")
    lon1 = rec1.get("lon")
    lat2 = rec2.get("lat")
    lon2 = rec2.get("lon")
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return False
    try:
        if haversine_distance(float(lat1), float(lon1), float(lat2), float(lon2)) >= 50.0:
            return False
    except (ValueError, TypeError):
        return False

    name1 = re.sub(r"\s+", "", str(rec1.get("name") or ""))
    name2 = re.sub(r"\s+", "", str(rec2.get("name") or ""))
    if not name1 or not name2:
        return False
    return (name1 in name2) or (name2 in name1)


def should_replace(new_rec: Dict[str, Any], old_rec: Dict[str, Any]) -> bool:
    """
    同一場去重保留規則：
    保留有 car_value 的那一筆；兩筆都有就保留 parkboss 那筆。
    """
    new_has = new_rec.get("car_value") is not None
    old_has = old_rec.get("car_value") is not None

    if new_has and not old_has:
        return True
    if old_has and not new_has:
        return False

    # 兩筆都有 car_value 或兩筆都沒有 car_value
    if new_rec.get("source") == "parkboss" and old_rec.get("source") != "parkboss":
        return True
    return False


def deduplicate_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """依去重規則合併重複場站"""
    unique: List[Dict[str, Any]] = []
    for rec in records:
        match_idx = -1
        for idx, existing in enumerate(unique):
            if is_same_parking_lot(rec, existing):
                match_idx = idx
                break
        if match_idx == -1:
            unique.append(rec)
        else:
            if should_replace(rec, unique[match_idx]):
                unique[match_idx] = rec
    return unique


def fetch_all(lat: float, lon: float, radius_m: float = 0.0) -> List[Dict[str, Any]]:
    """
    先跑 parkboss（維持現有行為與錯誤處理），
    再併行跑所有縣市政府 adapter。
    任何一個 adapter 失敗吞掉錯誤並向 stderr 印出 [warn] <source> 取不到：<原因>。
    最後經過去重規則回傳。
    """
    # 1. 先跑 parkboss
    pb_data = fetch_parking_spaces(lat, lon)

    # 2. 併行跑所有縣市 adapter
    adapter_records: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_source = {
            executor.submit(func): slug for slug, func in sources.ADAPTERS.items()
        }
        for future in concurrent.futures.as_completed(future_to_source):
            slug = future_to_source[future]
            try:
                recs = future.result()
                adapter_records.extend(recs)
            except Exception as e:
                sys.stderr.write(f"[warn] {slug} 取不到：{e}\n")
                sys.stderr.flush()

    # 3. TDX（第四層）——只在查詢座標落在它負責的縣才出手，且全程走快取節流。
    #    它跟其他 adapter 不同：需要座標（不能全台掃）、會撞限額、要憑證，
    #    所以不掛進 sources.ADAPTERS，單獨在這裡呼叫。
    tdx_records: List[Dict[str, Any]] = []
    try:
        import tdx as tdx_mod
        tdx_records = tdx_mod.fetch_tdx(lat, lon, radius_m)
    except Exception as e:
        sys.stderr.write(f"[warn] tdx 取不到：{e}\n")
        sys.stderr.flush()

    # 4. 合併與去重
    all_records = pb_data + adapter_records + tdx_records
    merged = deduplicate_records(all_records)

    # 5. 拿 TDX 的即時空位替既有紀錄補值。
    #    TDX 的 CarPark 與 ParkingAvailability 兩支端點對不齊（屏東實測 20 場 vs
    #    24 筆即時，其中 22 筆不在場資清單裡），那 22 筆沒有座標、自己成不了一筆場，
    #    但場名跟我方靜態庫對得上。只補「本來沒有即時值」的紀錄，不覆蓋任何既有數字。
    try:
        import tdx as tdx_mod
        overlay = tdx_mod.availability_overlay(lat, lon, radius_m)
    except Exception:
        overlay = []
    if overlay:
        _apply_availability_overlay(merged, overlay)
    return merged


# 這些核心名在台灣到處都是，跨鄉鎮同名是常態不是巧合。核心名落在這裡的，
# 名稱比對一律要求完全相同，不接受「包含」——因為 overlay 那批沒有座標，
# 只能靠名字判斷是不是同一個場，而這些名字根本不具辨識力。
_GENERIC_LOT_CORES = {
    "第一", "第二", "第三", "第四", "第五",
    "站前", "公有", "臨時", "收費", "免費", "公用",
    "體育館", "公園", "市場", "廣場", "漁港", "河濱",
    "火車站", "轉運站", "衛生所", "鄉公所", "鎮公所", "區公所", "圖書館",
}


def _apply_availability_overlay(records: List[Dict[str, Any]],
                                overlay: List[Dict[str, Any]]) -> None:
    """名稱比對補即時值。就地改 records。

    比對從嚴，寧可少補也不要補錯——補錯等於把別的場的空位數掛在這個場上：
      - 名稱去掉空白後要有一方完整包含另一方，且較短的那個至少 4 個字
        （少於 4 個字的名稱太容易誤中，例如「停四」）
      - 該紀錄的座標要落在這一筆所屬縣的範圍框裡
      - 只補 car_value 還是 None 的紀錄，已經有數字的一律不動
    """
    def norm(x: Any) -> str:
        return re.sub(r"\s+", "", str(x or ""))

    def core(name: str) -> str:
        """去掉尾巴的「停車場／停車位／地下停車場」這類共同後綴，留下辨識用的核心。"""
        return re.sub(r"(立體|地下|平面)?停車(場|位)$", "", name)

    for rec in records:
        if rec.get("car_value") is not None:
            continue
        rname = norm(rec.get("name"))
        if len(rname) < 4:
            continue
        rlat, rlon = rec.get("lat"), rec.get("lon")
        for a in overlay:
            aname = norm(a.get("name"))
            if len(aname) < 4:
                continue
            if rname == aname:
                pass  # 完全同名，最安全的一格
            elif rname in aname or aname in rname:
                # agy 紅隊 CRIT：只靠「一方包含另一方」＋全縣外框（跨度上百公里）
                # 會嚴重跨鄉鎮誤配——潮州的「第一停車場」會吃到 25 公里外
                # 屏東市「屏東公園第一停車場」的即時空位，枋寮的「站前停車場」
                # 會吃到「屏東站前停車場」。overlay 那批沒有座標，補錯就是把
                # 別的鎮的空位數掛在這個場上，比不補更糟。
                # 所以：核心名（去掉「停車場」後綴）若是通用詞，一律要求完全同名。
                shorter = rname if len(rname) <= len(aname) else aname
                if core(shorter) in _GENERIC_LOT_CORES or len(shorter) < 6:
                    continue
            else:
                continue
            box = a.get("box")
            if box and rlat is not None and rlon is not None:
                la0, la1, lo0, lo1 = box
                if not (la0 <= float(rlat) <= la1 and lo0 <= float(rlon) <= lo1):
                    continue
            rec["car_value"] = a.get("car_value")
            if rec.get("car_total") in (None, 0):
                rec["car_total"] = a.get("car_total")
            rec["car_time"] = a.get("car_time")
            break


SHORTLINK_LEDGER = os.path.expanduser("~/life-os/state/parking-shortlinks.json")


def shorten(url: str) -> str:
    """把導航長網址換成 s.life-os.work 短碼。換不到就原樣回長網址。

    **一定要走台帳**（`state/parking-shortlinks.json`）：後端每收到一次請求就發一個
    新短碼，同一個場查十次就在 KV 裡留十筆垃圾（實測同一條網址連打兩次拿到
    bj8dt 與 bgdni 兩個不同碼）。場的座標不會變，所以一個場一輩子只該有一個短碼。

    這一步失敗不該讓查詢失敗——沒有金鑰檔、沒網路、後端回怪東西，
    一律安靜退回長網址，使用者照樣點得開，只是長一點。
    """
    try:
        with open(SHORTLINK_LEDGER, encoding="utf-8") as f:
            ledger = json.load(f)
    except Exception:
        ledger = {}
    if url in ledger:
        return ledger[url]

    script = os.path.expanduser("~/life-os/scripts/movie-shortlink.sh")
    if not os.path.exists(script):
        return url
    try:
        out = subprocess.run(["bash", script, url], capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except Exception:
        return url
    if not out.startswith("https://s.life-os.work/"):
        return url
    ledger[url] = out
    # agy 紅隊 HIGH：原本直接覆寫台帳，中途被打斷或兩個行程同時寫，
    # 讀回來就是半截 JSON，上面那個 except 會把它整份當空 dict——
    # 歷史短碼全毀且零告警。寫暫存再 os.replace 換過去。
    try:
        os.makedirs(os.path.dirname(SHORTLINK_LEDGER), exist_ok=True)
        tmp = f"{SHORTLINK_LEDGER}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SHORTLINK_LEDGER)
    except Exception:
        pass  # 台帳寫不進去只是下次會多發一個碼，不該讓查詢失敗
    return out


def geocode_address(address: str) -> Tuple[float, float]:
    """呼叫既有 ~/bin/gmaps geocode 取得座標"""
    gmaps_bin = os.path.expanduser("~/bin/gmaps")
    if not os.path.exists(gmaps_bin):
        gmaps_bin = "/Users/applyao/bin/gmaps"
    if not os.path.exists(gmaps_bin):
        sys.stderr.write(f"gmaps binary not found at {gmaps_bin}\n")
        sys.exit(1)

    env = os.environ.copy()
    if "LIFEOS_CHANNEL_DIR" not in env and os.path.exists("/Users/applyao/.claude/channels/line"):
        env["LIFEOS_CHANNEL_DIR"] = "/Users/applyao/.claude/channels/line"
    if env.get("HOME", "").startswith("/Users/applyao/.agy-stores"):
        env["HOME"] = "/Users/applyao"

    # 先嘗試以 --json 取得精確結構化結果
    try:
        proc = subprocess.run(
            [gmaps_bin, "geocode", address, "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            results = data.get("results", [])
            if results:
                loc = results[0].get("geometry", {}).get("location", {})
                if "lat" in loc and "lng" in loc:
                    return float(loc["lat"]), float(loc["lng"])
    except Exception:
        pass

    # 若 json 失敗，fallback 解析預設文字格式（名稱｜地址｜座標）
    try:
        proc = subprocess.run(
            [gmaps_bin, "geocode", address],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gmaps geocode failed"
            sys.stderr.write(f"gmaps geocode error: {err}\n")
            sys.exit(1)

        out = proc.stdout.strip()
        if "查無結果" in out or not out:
            sys.stderr.write(f"地址查無座標: {address}\n")
            sys.exit(1)

        for line in out.splitlines():
            parts = line.split("｜")
            if len(parts) >= 3:
                coords = parts[2].strip().split(",")
                if len(coords) == 2:
                    return float(coords[0]), float(coords[1])
    except Exception as e:
        sys.stderr.write(f"Failed to geocode address: {e}\n")
        sys.exit(1)

    sys.stderr.write(f"地址查無座標: {address}\n")
    sys.exit(1)


AVAIL_LABELS = {
    # 一個門檻表兩個出口共用：顯示層拿 emoji／文字，JSON 層拿英文狀態碼。
    # 門檻寫在這裡而不是各寫一份，是因為 2026-09-08 的建議正好點到這件事——
    # 燈號規則以後會改（例如綠燈改成 10 格以上），改一個地方就好。
    "unknown": ("❓", "空位不明", "unknown"),
    "full":    ("🔴", "已滿", "full"),
    "low":     ("🟡", "快滿", "low"),
    "green":   ("🟢", "有位", "available"),
}


def availability_key(value, expired: bool) -> str:
    """把剩餘車位數收成四種狀態之一。expired＝時間戳過期，一律當不明。"""
    if expired or value is None:
        return "unknown"
    try:
        v = int(value)
    except (ValueError, TypeError):
        return "unknown"
    if v == 0:
        return "full"
    if v <= 2:
        return "low"
    return "green"


def process_and_output(data: List[Dict[str, Any]], center_lat: float, center_lon: float, radius: float, min_total: int, as_json: bool, limit: int = 3, include_moto: bool = False, plain: bool = False):
    """過濾、計算新鮮度與距離並格式化輸出"""
    now = datetime.now()
    results = []

    for item in data:
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            continue
        try:
            d = haversine_distance(center_lat, center_lon, float(lat), float(lon))
        except (ValueError, TypeError):
            continue

        if d > radius:
            continue

        # 「沒公布總格數」不等於「只有 0 格」——縣府靜態資料多半不附容量，
        # 舊寫法把 None 當 0 拿去比 min_total，會把整批合法場站安靜刪掉
        # （南投 71、恆春 40、雲林 3 全數消失且零告警，2026-09-08 實測）。
        # 只有「知道容量而且真的小於門檻」才過濾掉。
        # 機車專用場濾掉（2026-09-08 板橋實測：「湳仔溝機車平面停車場」被排到第二名）。
        # 判準只看名字帶「機車」而且沒帶「汽」——「汽機車停車場」那種是兩種都收，
        # 不能一起濾掉。名字之外沒有可靠的車種欄位可用（parkboss 不給），
        # 所以這是盡力而為的過濾，不是保證；`--include-moto` 給要看機車格的人。
        _n = str(item.get("name") or "")
        if not include_moto and "機車" in _n and "汽" not in _n:
            continue

        # agy 紅隊 HIGH 補強：0 也要當「未知」。有些來源對不知道容量的場填 0 而不是
        # null，照舊寫法會把「總格數 0 但即時空位 15 格」的合法場站安靜殺掉。
        car_total = item.get("car_total")
        if car_total not in (None, 0):
            try:
                if int(car_total) < min_total:
                    continue
            except (ValueError, TypeError):
                pass

        freshness = calculate_freshness(item.get("car_time"), now=now)
        rate_summary = extract_rate_summary(item.get("pay_info"))

        item_copy = dict(item)
        item_copy["distance_m"] = int(round(d))
        item_copy["geo_precision"] = item.get("geo_precision", "exact")
        item_copy["fresh_level"] = freshness["fresh_level"]
        item_copy["fresh_text"] = freshness["fresh_text"]
        item_copy["freshness"] = freshness["fresh_text"]
        item_copy["rate_summary"] = rate_summary
        item_copy["_internal_freshness"] = freshness

        results.append(item_copy)

    # 排序（2026-09-08 伊森裁示「停車場找有空位的優先（查得到的話）」）：
    # 第一鍵＝空位狀態，第二鍵才是距離。三態的順序是刻意的——
    #   0 查得到而且還有空位  → 最有用
    #   1 查不到空位          → 不知道，但去了可能有
    #   2 查得到但是滿的       → 去了一定白跑，排最後
    # 「滿的」排在「不知道」後面，是因為已知為 0 比未知更糟，不是排序寫反了。
    def _avail_rank(item):
        v = item.get("car_value")
        if item["_internal_freshness"]["is_expired"] or v is None:
            return 1
        try:
            return 0 if int(v) > 0 else 2
        except (ValueError, TypeError):
            return 1

    # agy 紅隊 HIGH：純「有空位優先」沒有距離上限，950 公尺外只剩 1 格的場會把
    # 30 公尺處的大場擠出前三名——在市區開車那叫捨近求遠，而且遠場那 1 格
    # 開到的時候多半已經被停走。所以先分距離帶（500 公尺一帶）再比空位：
    # 同一帶內有空位的排前面，不同帶就是近的贏。500 這個數字＝走路可接受的範圍，
    # 帶內換場等於「停這邊或停那邊都差不多」，此時才輪到空位當判準。
    BAND_M = 500
    results.sort(key=lambda x: (x["distance_m"] // BAND_M, _avail_rank(x), x["distance_m"]))

    # --json 一律給全部，不套 limit：那是給程式吃的，截斷會讓下游以為附近只有三個場。
    if as_json:
        for r in results:
            fr = r.pop("_internal_freshness", None)
            # 導航連結與空位狀態原本只長在給人看的那條路上，程式吃 --json 的人
            # 得自己重拼一次 Google Maps 網址、自己重寫一次燈號門檻——那是同一份
            # 判準散成兩份的開始。這裡補進 JSON，短網址不在此列（那要打我們自己的
            # 服務，95 筆逐一發碼沒有意義，需要的人拿 navigation_url 自己縮）。
            r["navigation_url"] = (
                "https://www.google.com/maps/dir/?api=1&destination="
                f"{r['lat']},{r['lon']}"
            )
            expired = bool(fr and fr.get("is_expired"))
            r["availability_status"] = AVAIL_LABELS[
                availability_key(r.get("car_value"), expired)
            ][2]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if not results:
        r_disp = int(radius) if isinstance(radius, (int, float)) and float(radius).is_integer() else radius
        print(f"半徑 {r_disp} 公尺內沒有回報車位的停車場")
        sys.exit(0)

    # 排列式輸出（2026-09-08 伊森裁示：「最好是排列式 後面接可以點擊的
    # google map 地址 URL」）。一場一段：編號＋場名一行、距離／車位／鮮度／費率一行、
    # 導航連結一行。他多半在開車，連結用 `maps/dir/?api=1&destination=` 直接進導航，
    # 不用 `maps/search`（那只是把地圖打開還要再按一次）。
    # 只端前 N 個（2026-09-08 伊森裁示「停車場最好是給三個」）：他在開車，
    # 一次看三個已經是極限，列十個等於沒列。被截掉的數量講一句就好。
    shown = results if limit <= 0 else results[:limit]
    for idx, item in enumerate(shown, 1):
        freshness = item["_internal_freshness"]
        name = item.get("name") or ""
        car_total = item.get("car_total")
        # 總格數 0 跟 None 一樣是「沒公布」，不是「這裡有 0 格」——印成
        # 「181/0」會讓人以為資料壞了（2026-09-08 板橋實測）。沒有分母就只給分子。
        total_str = str(car_total) if car_total not in (None, 0) else None

        # 燈號（2026-09-08 伊森裁示）：綠＝3 個以上、黃＝剩 1-2、紅＝0、問號＝查不到。
        # 他在開車，一眼看顏色就夠了，數字是給想確認的人看的第二層。
        if freshness["is_expired"]:
            car_val = None
        else:
            car_val = item.get("car_value")
        try:
            v = int(car_val) if car_val is not None else None
        except (ValueError, TypeError):
            v = None
        # `--plain` 走純文字燈號，給不吃 emoji 的出口用。
        # ⚠️ 送 LINE **不要加**：🟢🟡🔴❓ 這四顆 2026-09-08 12:33 起已從
        # EMOJICAP 計數排除（hooks/line-tone-gate-check.py 的 STATUS_LIGHTS），
        # 17:1x 拿三場真實輸出離線複驗，沒有 EMOJICAP。本註解原本寫著相反的話，
        # 是排除生效之前的舊狀況。
        key = availability_key(v, False)
        emoji, word, _ = AVAIL_LABELS[key]
        light = word if plain else emoji
        val_str = "--" if key == "unknown" else str(v)
        # 空位比總格數還多＝來源自己的資料互相矛盾（2026-09-08 板橋實測：
        # 東方富域 parkboss 回 avail 353／total 184）。印成「353/184」看起來像
        # 程式壞了，而且分母是錯的那一半——即時空位每分鐘更新、總格數是靜態欄位。
        # 處置：矛盾時丟掉分母只留分子，不猜哪個對、也不把整場濾掉
        # （那會讓真的有位的場消失）。
        if total_str is not None and v is not None:
            try:
                if v > int(car_total):
                    total_str = None
            except (ValueError, TypeError):
                pass

        if total_str is None:
            space_str = f"{light} {val_str}" if v is not None else f"{light}"
        else:
            space_str = f"{light} {val_str}/{total_str}"

        # 座標是猜出來的就不要讓距離看起來像量出來的：地號類地址 Google 會退回
        # 鄉鎮中心點，一整批場會顯示成同一個距離（實測竹北 66 場撞同一點）。
        if item.get("geo_precision") == "approx":
            dist_str = f"約 {item['distance_m']}m(位置概略)"
        else:
            dist_str = f"{item['distance_m']}m"

        line2 = f"   {dist_str}｜{space_str}｜{freshness['fresh_text']}"
        rate_str = item["rate_summary"]
        if rate_str and rate_str.strip() and rate_str != "--":
            line2 += f"｜{rate_str}"

        nav = (f"https://www.google.com/maps/dir/?api=1&destination="
               f"{item['lat']},{item['lon']}")
        print(f"{idx}. {name}")
        print(line2)
        print(f"   {shorten(nav)}")
        if idx != len(shown):
            print()

    if len(results) > len(shown):
        # 逗號會被 LINE 語氣閘的 PUNCT 擋下（送 LINE 是原樣轉貼，一個字不改），
        # 所以斷句用空白（2026-09-08 17:1x 實測，此前這一行讓整則過不了閘）。
        print(f"\n（半徑內另有 {len(results) - len(shown)} 個 "
              f"要看全部加 --limit 0）")


def main():
    parser = argparse.ArgumentParser(description="即時停車位查詢工具")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # near
    near_parser = subparsers.add_parser("near", help="依經緯度查詢附近停車場")
    near_parser.add_argument("lat", type=float, help="緯度")
    near_parser.add_argument("lon", type=float, help="經度")
    near_parser.add_argument("--radius", type=float, default=1000.0, help="搜尋半徑（公尺，預設 1000）")
    near_parser.add_argument("--limit", type=int, default=3, help="最多列幾個（預設 3；0＝全部）")
    near_parser.add_argument("--include-moto", action="store_true", help="連機車專用場也列（預設濾掉）")
    near_parser.add_argument("--min-total", type=int, default=6, help="最小總車位數（預設 6）")
    near_parser.add_argument("--plain", action="store_true", help="燈號用文字（有位／快滿／已滿／無資料），送 LINE 用")
    near_parser.add_argument("--json", action="store_true", help="輸出 JSON 格式")

    # near-address
    addr_parser = subparsers.add_parser("near-address", help="依地址或地標查詢附近停車場")
    addr_parser.add_argument("address", type=str, help="地址或地標")
    addr_parser.add_argument("--radius", type=float, default=1000.0, help="搜尋半徑（公尺，預設 1000）")
    addr_parser.add_argument("--limit", type=int, default=3, help="最多列幾個（預設 3；0＝全部）")
    addr_parser.add_argument("--include-moto", action="store_true", help="連機車專用場也列（預設濾掉）")
    addr_parser.add_argument("--min-total", type=int, default=6, help="最小總車位數（預設 6）")
    addr_parser.add_argument("--plain", action="store_true", help="燈號用文字（有位／快滿／已滿／無資料），送 LINE 用")
    addr_parser.add_argument("--json", action="store_true", help="輸出 JSON 格式")

    args = parser.parse_args()

    if args.subcommand == "near":
        lat, lon = args.lat, args.lon
    elif args.subcommand == "near-address":
        lat, lon = geocode_address(args.address)
    else:
        parser.print_help()
        sys.exit(2)

    raw_data = fetch_all(lat, lon, args.radius)
    process_and_output(
        data=raw_data,
        center_lat=lat,
        center_lon=lon,
        radius=args.radius,
        min_total=args.min_total,
        as_json=args.json,
        limit=args.limit,
        include_moto=args.include_moto,
        plain=args.plain
    )


if __name__ == "__main__":
    main()
