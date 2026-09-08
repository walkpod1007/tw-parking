#!/usr/bin/env python3
"""tdx.py — 交通部 TDX 運輸資料流通服務的停車場來源（第四層）。

為什麼有這一層：
苗栗與金門兩縣**沒有任何縣府自有端點**（路邊停車全委外給民間業者），
parkboss 也不收，所以前三層對這兩縣一律回「沒有停車場」——實測在高鐵苗栗站旁邊
查詢會拿到 0 場，而那裡一公里內實際上有四個。
另外屏東雖然有縣府靜態資料，即時空位卻只有 TDX 有（實測 24 筆）。

**這一層跟前三層最大的不同是它會撞限額**：免費方案實測連打第 6 發就 429，
所以這裡的規矩是「能不打就不打」——

  1. token 存檔重用（TDX 的 token 有效期以回應的 expires_in 為準，提前 60 秒到期）
  2. 只打**查詢座標所在的那一個縣**，不是把全台掃一遍
  3. 靜態場資快取 7 天（場名座標費率一年變不了幾次）
  4. 即時空位快取 60 秒——一百個人同一分鐘查同一個場，對外只有一發

沒有憑證檔、抓失敗、或該縣沒資料，一律安靜回空清單，不讓第四層的問題
波及前三層已經查得到的結果。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

AUTH_URL = ("https://tdx.transportdata.tw/auth/realms/TDXConnect"
            "/protocol/openid-connect/token")
API_BASE = "https://tdx.transportdata.tw/api/basic/v1/Parking/OffStreet"
# TDX 憑證：先看環境變數 TDX_CLIENT_ID / TDX_CLIENT_SECRET，沒有才讀憑證檔。
# 憑證檔位置可用 TDX_CRED_FILE 改；預設放在 ~/.config/parking/tdx.env。
# 兩個都沒有也能跑——TDX 只是第四層來源，前三層公開端點不需要任何金鑰。
# 預設會依序找這幾個位置，第一個存在的就用。多一條 ~/.secrets/tdx.env 是因為
# 2026-09-08 註冊拿到的金鑰落在那裡，而程式只找 ~/.config/parking/tdx.env，
# 結果 TDX 這一層整天靜默沒出力（苗栗與金門因此一直是零場，看起來像涵蓋問題，
# 實際是憑證放在程式沒去找的地方）。TDX_CRED_FILE 指定時只用指定的那個。
_CRED_CANDIDATES = ["~/.config/parking/tdx.env", "~/.secrets/tdx.env"]


def _pick_cred_file() -> str:
    env = os.environ.get("TDX_CRED_FILE", "").strip()
    if env:
        return os.path.expanduser(env)
    for c in _CRED_CANDIDATES:
        path = os.path.expanduser(c)
        if os.path.exists(path):
            return path
    return os.path.expanduser(_CRED_CANDIDATES[0])


CRED_FILE = _pick_cred_file()
CACHE_DIR = os.path.expanduser(
    os.environ.get("PARKING_CACHE_DIR", "~/.cache/parking") + "/tdx")

STATIC_TTL = 7 * 24 * 3600   # 場資：一週
AVAIL_TTL = 60               # 即時空位：一分鐘

# 只掛前三層**查不到或缺即時值**的縣；每多掛一個縣就多一份撞限額的機會。
#   苗栗、金門 — 前三層完全查無
#   屏東       — 靜態有了，即時空位只有 TDX 有
# 範圍框是粗略的縣界外接矩形，只用來決定「這個座標要問哪一個縣」，
# 不當作行政區判定；框重疊時兩個縣都問（各自有自己的快取，不會重複打）。
CITY_BOXES: List[Tuple[str, float, float, float, float]] = [
    # (TDX City 代碼, lat_min, lat_max, lon_min, lon_max)
    ("MiaoliCounty",   24.28, 24.78, 120.58, 121.18),
    ("PingtungCounty", 21.85, 22.92, 120.32, 120.98),
    ("KinmenCounty",   24.34, 24.58, 118.16, 118.52),
]


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _read_cache(name: str, ttl: int) -> Optional[Any]:
    p = _cache_path(name)
    try:
        if time.time() - os.path.getmtime(p) > ttl:
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(name: str, data: Any) -> None:
    """原子寫入。

    紅隊審查 HIGH：原本直接 `open(path, "w")`，那會先把檔案截成 0 再慢慢寫；
    另一個行程剛好在那個瞬間讀，拿到的是空檔或半截 JSON → `JSONDecodeError`
    → 快取被判定失效 → 全部穿透去打 TDX，而 TDX 第 6 發就 429。
    寫暫存檔再 `os.replace` 換過去，讀的人永遠看到完整的舊版或完整的新版。
    """
    path = _cache_path(name)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # 暫存檔清不掉不該讓查詢失敗


def _load_credentials() -> Optional[Tuple[str, str]]:
    """讀 TDX client id/secret：環境變數優先，其次憑證檔。憑證真值不進版控。"""
    env_cid = os.environ.get("TDX_CLIENT_ID", "").strip()
    env_secret = os.environ.get("TDX_CLIENT_SECRET", "").strip()
    if env_cid and env_secret:
        return env_cid, env_secret
    if not os.path.exists(CRED_FILE):
        return None
    cid = secret = None
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().replace("export ", "").strip()
                v = v.strip().strip('"').strip("'")
                if k == "TDX_CLIENT_ID":
                    cid = v
                elif k == "TDX_CLIENT_SECRET":
                    secret = v
    except Exception:
        return None
    if cid and secret:
        return cid, secret
    return None


def _get_token() -> Optional[str]:
    cached = _read_cache("token.json", ttl=10 ** 9)  # 有效期看檔案內的 expires_at
    if cached and cached.get("expires_at", 0) > time.time():
        return cached.get("access_token")

    cred = _load_credentials()
    if not cred:
        return None
    cid, secret = cred
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode()
    req = urllib.request.Request(
        AUTH_URL, data=body,
        headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    token = data.get("access_token")
    if not token:
        return None
    # 提前 60 秒過期，免得剛好卡在邊界拿到一張已經失效的
    # 紅隊審查 LOW：原本寫 `max(ttl, 60)`，若 TDX 回的 expires_in <= 60，
    # 減 60 之後是負數卻被 max 拉回 60 秒——等於在 token 已經失效之後
    # 還硬存 60 秒繼續送，換來一串 401。負數就是別存。
    ttl = int(data.get("expires_in", 86400)) - 60
    if ttl > 0:
        _write_cache("token.json", {"access_token": token,
                                    "expires_at": time.time() + ttl})
    return token


def _api_get(path: str, token: str) -> Optional[Any]:
    url = f"{API_BASE}/{path}?%24format=JSON"
    req = urllib.request.Request(url, headers={
        "authorization": f"Bearer {token}",
        "accept-encoding": "identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def cities_for(lat: float, lon: float, radius_m: float = 0.0) -> List[str]:
    """這個查詢範圍碰得到哪幾個縣。空清單＝這一層不必出手。

    紅隊審查 MED：原本只判「座標這一點」在不在框裡，人站在縣界外緣就整縣不查——
    例如在新竹香山（24.785）用半徑 1000 公尺查，苗栗竹南那邊 700 公尺內的場全數漏掉，
    而且是安靜漏掉。改成把查詢點依半徑外擴成一個方框，再跟縣框做矩形相交。
    """
    # 緯度 1 度 ≈ 111km；經度隨緯度收縮，台灣約 cos(24°)≈0.914
    dlat = radius_m / 111_000.0
    dlon = radius_m / (111_000.0 * 0.914) if radius_m else 0.0
    qla0, qla1 = lat - dlat, lat + dlat
    qlo0, qlo1 = lon - dlon, lon + dlon
    out = []
    for city, la0, la1, lo0, lo1 in CITY_BOXES:
        if qla0 <= la1 and qla1 >= la0 and qlo0 <= lo1 and qlo1 >= lo0:
            out.append(city)
    return out


def _carparks(city: str, token: str) -> List[Dict[str, Any]]:
    cached = _read_cache(f"carpark-{city}.json", STATIC_TTL)
    if cached is not None:
        return cached
    data = _api_get(f"CarPark/City/{city}", token)
    if not isinstance(data, dict):
        return []
    parks = data.get("CarParks", [])
    _write_cache(f"carpark-{city}.json", parks)
    return parks


def _availability(city: str, token: str) -> Dict[str, Dict[str, Any]]:
    """回 {CarParkID: 該場的即時空位紀錄}。該縣沒有即時回報時回空 dict。"""
    cached = _read_cache(f"avail-{city}.json", AVAIL_TTL)
    if cached is None:
        data = _api_get(f"ParkingAvailability/City/{city}", token)
        if not isinstance(data, dict):
            return {}
        cached = data.get("ParkingAvailabilities", [])
        _write_cache(f"avail-{city}.json", cached)
    return {a.get("CarParkID"): a for a in cached if a.get("CarParkID")}


def fetch_tdx(lat: float, lon: float, radius_m: float = 0.0) -> List[Dict[str, Any]]:
    """查詢範圍碰到的縣的 TDX 停車場。任何一步失敗都回空清單，不拋例外。

    紅隊審查 HIGH：失敗時安靜回 `[]`，在苗栗與金門會直接變成「半徑內沒有停車場」，
    駕駛會以為周遭真的沒有場站而放棄。所以拿不到憑證或拿不到 token 時往 stderr
    出一聲——不打斷查詢（前三層的結果照常給），但讓人知道這一層沒出力。
    """
    cities = cities_for(lat, lon, radius_m)
    if not cities:
        return []
    token = _get_token()
    if not token:
        sys.stderr.write("[warn] tdx 這一層沒出力：拿不到 token（憑證檔缺、過期或撞限額）"
                         "——苗栗／金門的結果會少掉，不代表附近真的沒有停車場\n")
        sys.stderr.flush()
        return []

    out: List[Dict[str, Any]] = []
    for city in cities:
        parks = _carparks(city, token)
        if not parks:
            continue
        avail = _availability(city, token)
        for p in parks:
            pos = p.get("CarParkPosition") or {}
            plat, plon = pos.get("PositionLat"), pos.get("PositionLon")
            if plat is None or plon is None:
                continue
            name = (p.get("CarParkName") or {}).get("Zh_tw") or p.get("CarParkID")
            a = avail.get(p.get("CarParkID")) or {}
            # TotalSpaces 在場資裡常常是 None，即時那份反而有；兩邊都取。
            total = p.get("TotalSpaces")
            if total in (None, 0):
                total = a.get("TotalSpaces")
            out.append({
                "name": name,
                "lat": float(plat),
                "lon": float(plon),
                "car_total": total,
                "car_value": a.get("AvailableSpaces"),
                "car_time": a.get("DataCollectTime"),
                "pay_info": p.get("FareDescription"),
                "source": f"tdx:{city}",
                "geo_precision": "exact",
            })
    return out


def availability_overlay(lat: float, lon: float, radius_m: float = 0.0) -> List[Dict[str, Any]]:
    """回該縣「有即時空位、但不在 CarPark 靜態清單裡」的那些場。

    TDX 這兩支端點對不齊——實測屏東縣 CarPark 只有 20 場，ParkingAvailability
    卻有 24 筆，其中 **22 筆的 CarParkID 不在 CarPark 清單裡**（中華路立體、
    信義路立體、縣府立體這些最常用的都在這一批）。這些筆**沒有座標**，
    自己不能變成一筆可查距離的場，但它們的場名跟本地靜態庫裡的場對得上，
    所以拿來替既有紀錄補上「還剩幾格」。

    回的是 {name, car_value, car_total, car_time, city_box} 的清單，
    由呼叫端負責比對名稱與範圍——比對規則放在呼叫端，這裡只負責取資料。
    """
    cities = cities_for(lat, lon, radius_m)
    if not cities:
        return []
    token = _get_token()
    if not token:
        return []
    out: List[Dict[str, Any]] = []
    for city in cities:
        parks = _carparks(city, token)
        known = {p.get("CarParkID") for p in parks}
        avail = _availability(city, token)
        box = next((b for b in CITY_BOXES if b[0] == city), None)
        for cid, a in avail.items():
            if cid in known:
                continue  # 已經由 fetch_tdx 那條路帶進來了
            name = (a.get("CarParkName") or {}).get("Zh_tw")
            if not name:
                continue
            out.append({
                "name": name,
                "car_value": a.get("AvailableSpaces"),
                "car_total": a.get("TotalSpaces"),
                "car_time": a.get("DataCollectTime"),
                "box": box[1:] if box else None,
            })
    return out
