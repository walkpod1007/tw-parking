#!/usr/bin/env python3
"""
縣市政府自有停車端點 Adapters
"""

import sys
import ssl
import json
import os
import csv
import io
import re
import html
import urllib.request
import urllib.error
import urllib.parse
import concurrent.futures
from datetime import datetime
from typing import Optional, Dict, Any, List


# 這幾個政府站的 TLS 設定過不了 Python 預設驗證（2026-09-08 逐一實測）：
#   www-ws.pthg.gov.tw / chpark.chcg.gov.tw / trafficweb.ttcpb.gov.tw
#   parking.nantou.gov.tw / zytparking.com → CERTIFICATE_VERIFY_FAILED（鏈不完整）
#   ws-tm.cyhg.gov.tw                      → DH_KEY_TOO_SMALL
# parking.yunlin.gov.tw 與 ws.hsinchu.gov.tw 用嚴格驗證就過得去，不放寬。
# **放寬只對名單內的主機生效**——寫成名單而不是全域關掉，是為了讓以後新增的來源
# 不會安靜地跟著吃 CERT_NONE；新來源要嘛憑證是好的，要嘛得有人親手把它加進這張名單。
RELAXED_TLS_HOSTS = {
    "www-ws.pthg.gov.tw",
    "ws-tm.cyhg.gov.tw",
    "chpark.chcg.gov.tw",
    "trafficweb.ttcpb.gov.tw",
    "parking.nantou.gov.tw",
    "traffic.hl.gov.tw",
    "zytparking.com",
}


def create_ssl_context(url: str = "") -> ssl.SSLContext:
    host = urllib.parse.urlsplit(url).hostname or ""
    ctx = ssl.create_default_context()
    if host not in RELAXED_TLS_HOSTS:
        return ctx
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:
        try:
            ctx.set_ciphers("DEFAULT:!DH")
        except Exception:
            pass
    return ctx


def fetch_url(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> bytes:
    h = {"User-Agent": "Mozilla/5.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    ctx = create_ssl_context(url)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
        return resp.read()


def sanitize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    髒值防線：
    - car_value 為負數、或大於 car_total（且兩者都不是 None）→ 把 car_value 設成 None，
      並在 car_time 之外多加一個鍵 dirty_reason（字串，說明為什麼被擋）。
    - car_total 為 0 或負數 → car_total 設 None。
    """
    car_val = rec.get("car_value")
    car_tot = rec.get("car_total")

    if car_val is not None:
        if car_val < 0:
            rec["car_value"] = None
            rec["dirty_reason"] = f"剩餘車位為負數 ({car_val})"
        elif car_tot is not None and car_val > car_tot:
            rec["car_value"] = None
            rec["dirty_reason"] = f"剩餘車位 ({car_val}) 大於總格數 ({car_tot})"

    if car_tot is not None and car_tot <= 0:
        rec["car_total"] = None

    return rec


# ── 只有地址沒有座標的三個來源 ───────────────────────────────────────────
# 屏東縣路外、嘉義縣、新竹縣三份政府開放資料只給地址不給經緯度（2026-09-08 實測欄位確認），
# 距離查詢用不到。做法是把它們抓下來一次、地址轉成座標、存進 static-store.json，
# 之後查詢直接讀本地那份，零 API 呼叫。
# `*_raw()` 只負責抓與正規化欄位（含 address），轉座標與存檔在 build-static-store.py。

STATIC_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static-store.json")


def _int_or_none(v):
    v = (str(v) if v is not None else "").strip().replace(",", "")
    return int(v) if v.isdigit() else None


def fetch_pthg_offstreet_raw() -> List[Dict[str, Any]]:
    url = "https://www-ws.pthg.gov.tw/Upload/2015pthg/0/relfile/0/0/01d6b4f2-84a1-44d7-bf4e-158a70fabe4d.csv"
    s_ = fetch_url(url, timeout=20).decode("utf-8", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(s_)):
        name = (row.get("停車場名稱") or "").strip()
        addr = (row.get("停車場地址") or "").strip()
        if not name or not addr:
            continue
        out.append({"name": name, "address": addr,
                    "car_total": _int_or_none(row.get("停車格總數（含專用車位）-汽車")),
                    "pay_info": row.get("收費標準"), "source": "pthg_offstreet"})
    return out


def fetch_cyhg_raw() -> List[Dict[str, Any]]:
    url = "https://ws-tm.cyhg.gov.tw/001/Upload/0/relfile/0/0/63b9dee0-2c99-4868-9650-97bc3bc0fbca.csv"
    raw = fetch_url(url, timeout=20)
    try:
        s_ = raw.decode("cp950")
    except Exception:
        s_ = raw.decode("utf-8", errors="replace")
    # ⚠️ 這份 CSV **完全沒有地址欄**（2026-09-08 實測表頭：鄉鎮市／序號／停車場名稱／停放型式／
    # 各車種席數／營業時間／收費狀況），而且表頭因為欄名內含換行而橫跨五個實體行。
    # 所以地址是用「嘉義縣＋鄉鎮市＋停車場名稱」湊出來餵給地理編碼的，精度不如原生座標，
    # 轉不出來的那幾筆就留 lat=None 不進查詢結果，不要硬塞一個猜的座標。
    # 鄉鎮市那一欄是**合併儲存格**，只有每一組的第一列有值，後面幾列是空的，
    # 所以要往下承接（last_town），不然 108 列只認得出 15 列。
    out = []
    last_town = ""
    for row in csv.DictReader(io.StringIO(s_)):
        name = (row.get("停車場名稱") or "").strip()
        town = (row.get("鄉鎮市") or "").strip()
        if town:
            last_town = town
        town = town or last_town
        if not name or not town or name.endswith("合計") or town.endswith("合計"):
            continue
        addr = f"嘉義縣{town}{name}"
        out.append({"name": name, "address": addr,
                    "car_total": _int_or_none(row.get("小客車\n(席)") or row.get("小客車 (席)")),
                    "pay_info": row.get("收費狀況"), "source": "cyhg"})
    return out


def fetch_hsinchu_raw() -> List[Dict[str, Any]]:
    url = "https://ws.hsinchu.gov.tw/001/Upload/1/opendata/8774/271/18b31a65-54c3-4fb6-93c4-63bf021e1bb6.json"
    data = json.loads(fetch_url(url, timeout=20).decode("utf-8"))
    rows = data if isinstance(data, list) else list(data.values())[0]
    out = []
    for item in rows:
        name = (item.get("停車場名稱") or "").strip()
        addr = (item.get("停車場地址-地號") or "").strip()
        if not name or not addr:
            continue
        out.append({"name": name, "address": addr,
                    "car_total": _int_or_none(item.get("停車格數量")),
                    "pay_info": item.get("收費方式"), "source": "hsinchu"})
    return out


def fetch_static_store() -> List[Dict[str, Any]]:
    """讀本地靜態庫。庫不存在或沒有座標的筆數一律略過，不打任何網路請求。"""
    if not os.path.exists(STATIC_STORE):
        return []
    with open(STATIC_STORE, encoding="utf-8") as f:
        store = json.load(f)
    out = []
    for r in store.get("records", []):
        if r.get("lat") is None or r.get("lon") is None:
            continue
        rec = sanitize({
            "name": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
            "car_total": r.get("car_total"), "car_value": None, "car_time": None,
            "pay_info": r.get("pay_info"), "source": r.get("source", "static"),
        })
        # 靜態庫的座標是地址轉來的，有一批是 Google 猜的鄉鎮中心點（地號查不到門牌）。
        # 這一欄要一路帶到輸出，否則距離看起來精準其實是假的。
        rec["geo_precision"] = r.get("geo_precision", "unknown")
        out.append(rec)
    return out


# 1. 屏東縣路外 (pthg_offstreet)
def fetch_pthg_offstreet() -> List[Dict[str, Any]]:
    url = "https://www-ws.pthg.gov.tw/Upload/2015pthg/0/relfile/0/0/01d6b4f2-84a1-44d7-bf4e-158a70fabe4d.csv"
    raw = fetch_url(url, timeout=20)
    s = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(s))
    results = []
    for row in reader:
        name = (row.get("停車場名稱") or "").strip()
        # 來源給地址沒給座標，依規範直接丟掉
        lat = row.get("lat") or row.get("entranceLatitude") or row.get("latitude")
        lon = row.get("lon") or row.get("entranceLongitude") or row.get("longitude")
        if not lat or not lon:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except ValueError:
            continue
        tot_raw = row.get("停車格總數（含專用車位）-汽車")
        car_tot = int(tot_raw) if tot_raw and tot_raw.isdigit() else None
        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": car_tot,
            "car_value": None,
            "car_time": None,
            "pay_info": row.get("收費標準"),
            "source": "pthg_offstreet",
        }
        results.append(sanitize(rec))
    return results


# 2. 屏東縣恆春 (pthg_hengchun)
def fetch_pthg_hengchun() -> List[Dict[str, Any]]:
    url = "https://www-ws.pthg.gov.tw/Upload/2015pthg/0/relfile/0/0/0a83a598-d9a2-4a00-814c-9c255ea02ae9.csv"
    raw = fetch_url(url, timeout=20)
    s = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(s))
    results = []
    for row in reader:
        name = (row.get("parkingLotName") or "").strip()
        lat = row.get("entranceLatitude") or row.get("exitLatitude")
        lon = row.get("entranceLongitude") or row.get("exitLongitude")
        if not name or not lat or not lon:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except ValueError:
            continue
        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": None,
            "car_value": None,
            "car_time": None,
            "pay_info": None,
            "source": "pthg_hengchun",
        }
        results.append(sanitize(rec))
    return results


# 3. 嘉義縣 (cyhg)
def fetch_cyhg() -> List[Dict[str, Any]]:
    url = "https://ws-tm.cyhg.gov.tw/001/Upload/0/relfile/0/0/63b9dee0-2c99-4868-9650-97bc3bc0fbca.csv"
    raw = fetch_url(url, timeout=20)
    try:
        s = raw.decode("cp950")
    except Exception:
        s = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(s))
    results = []
    for row in reader:
        name = (row.get("停車場名稱") or "").strip()
        lat = row.get("lat") or row.get("latitude")
        lon = row.get("lon") or row.get("longitude")
        if not lat or not lon:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except ValueError:
            continue
        tot_raw = row.get("小客車\n(席)") or row.get("小客車 (席)")
        car_tot = int(tot_raw) if tot_raw and tot_raw.isdigit() else None
        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": car_tot,
            "car_value": None,
            "car_time": None,
            "pay_info": row.get("收費狀況"),
            "source": "cyhg",
        }
        results.append(sanitize(rec))
    return results


# 4. 彰化縣 (chcg)
def fetch_chcg() -> List[Dict[str, Any]]:
    url = "https://chpark.chcg.gov.tw/ParkingLocation/ParkingLotPost"
    raw = fetch_url(url, method="POST", data=b"", timeout=20)
    data = json.loads(raw.decode("utf-8"))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = []
    for item in data:
        name = (item.get("carParkName") or "").strip()
        lat = item.get("positionLat")
        lon = item.get("positionLon")
        if not name or lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (ValueError, TypeError):
            continue

        total_raw = item.get("totalNum")
        car_total = None
        if total_raw not in (None, "", "-"):
            try:
                car_total = int(str(total_raw).strip())
            except ValueError:
                car_total = None

        rem_raw = item.get("remaining")
        car_value = None
        car_time = None
        if rem_raw not in (None, "", "-"):
            try:
                car_value = int(str(rem_raw).strip())
                car_time = now_str
            except ValueError:
                car_value = None

        fare = item.get("fareDescription") or item.get("description")
        pay_info = str(fare).strip() if fare else None

        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": car_total,
            "car_value": car_value,
            "car_time": car_time,
            "pay_info": pay_info,
            "source": "chcg",
        }
        results.append(sanitize(rec))
    return results


# 5. 雲林縣 (yunlin)
def fetch_yunlin() -> List[Dict[str, Any]]:
    url = "https://parking.yunlin.gov.tw/ParkingLocation/ParkingLotPost"
    raw = fetch_url(url, method="POST", data=b"", timeout=20)
    data = json.loads(raw.decode("utf-8"))
    results = []
    for item in data:
        name = (item.get("carParkName") or "").strip()
        lat = item.get("positionLat")
        lon = item.get("positionLon")
        if not name or lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (ValueError, TypeError):
            continue
        fare = item.get("fareDescription") or item.get("description")
        pay_info = str(fare).strip() if fare else None
        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": None,
            "car_value": None,
            "car_time": None,
            "pay_info": pay_info,
            "source": "yunlin",
        }
        results.append(sanitize(rec))
    return results


# 6. 新竹縣 (hsinchu)
def fetch_hsinchu() -> List[Dict[str, Any]]:
    url = "https://ws.hsinchu.gov.tw/001/Upload/1/opendata/8774/271/18b31a65-54c3-4fb6-93c4-63bf021e1bb6.json"
    raw = fetch_url(url, timeout=20)
    data = json.loads(raw.decode("utf-8"))
    results = []
    for item in data:
        name = (item.get("停車場名稱") or "").strip()
        lat = item.get("lat") or item.get("latitude")
        lon = item.get("lon") or item.get("longitude")
        if not lat or not lon:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except ValueError:
            continue
        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": None,
            "car_value": None,
            "car_time": None,
            "pay_info": item.get("收費方式"),
            "source": "hsinchu",
        }
        results.append(sanitize(rec))
    return results


# 7. 花蓮縣 (hualien)
def fetch_hualien() -> List[Dict[str, Any]]:
    url = "https://traffic.hl.gov.tw/Home/_ParkingDetailPartialView"
    data = b"page=1&pageNumber=100&action=DynamicParking&currentGroup=1&dataModel[KeyWord]="
    raw = fetch_url(url, method="POST", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    html_text = raw.decode("utf-8", errors="replace")
    results = []
    items = html_text.split("parking_space_list_item")
    for block in items[1:]:
        m_name = re.search(r'class="parking_space_item_title"[^>]*>[\s\S]*?<a [^>]*>([^<]+)</a>', block)
        name = html.unescape(m_name.group(1).strip()) if m_name else None
        m_coords = re.search(r'q=([0-9.-]+),([0-9.-]+)', block)
        if not m_coords or not name:
            continue
        try:
            lat_f = float(m_coords.group(1))
            lon_f = float(m_coords.group(2))
        except ValueError:
            continue

        m_tot = re.search(r'總停車位：</div>\s*<div class="text_content">(\d+)</div>', block)
        car_total = int(m_tot.group(1)) if m_tot else None

        m_fee = re.search(r'收費方式：</div>\s*<div class="text_content">([^<]+)</div>', block)
        pay_info = html.unescape(m_fee.group(1).strip()) if m_fee else None

        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": car_total,
            "car_value": None,
            "car_time": None,
            "pay_info": pay_info,
            "source": "hualien",
        }
        results.append(sanitize(rec))
    return results


# 8. 台東縣 (taitung)
def fetch_taitung() -> List[Dict[str, Any]]:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = []

    # 8.1 路外停車場清單
    url_lots = "https://trafficweb.ttcpb.gov.tw/api/parking-lots"
    raw_lots = fetch_url(url_lots, timeout=20)
    lots_data = json.loads(raw_lots.decode("utf-8")).get("data", [])

    def _get_lot_detail(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        lid = item.get("id")
        if not lid:
            return None
        try:
            raw_d = fetch_url(f"https://trafficweb.ttcpb.gov.tw/api/parking-lots/{lid}", timeout=5)
            d = json.loads(raw_d.decode("utf-8")).get("data")
            if not d:
                return None
            name = d.get("name") or f"臺東停車場-{lid}"
            lat_f = float(d["lat"])
            lon_f = float(d["lng"])
            total = int(d["total"]) if d.get("total") is not None else None
            vacancy = int(d["vacancy"]) if d.get("vacancy") is not None else None
            rec = {
                "name": name,
                "lat": lat_f,
                "lon": lon_f,
                "car_total": total,
                "car_value": vacancy,
                "car_time": now_str if vacancy is not None else None,
                "pay_info": d.get("charge"),
                "source": "taitung",
            }
            return sanitize(rec)
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        lot_records = [r for r in ex.map(_get_lot_detail, lots_data) if r]
    results.extend(lot_records)

    # 8.2 智慧路邊停車格
    url_spaces = "https://trafficweb.ttcpb.gov.tw/api/parking-spaces"
    try:
        raw_spaces = fetch_url(url_spaces, timeout=10)
        spaces_data = json.loads(raw_spaces.decode("utf-8")).get("data", [])
        for sp in spaces_data:
            try:
                rec = {
                    "name": f"臺東智慧車格-{sp['id']}",
                    "lat": float(sp["lat"]),
                    "lon": float(sp["lng"]),
                    "car_total": 1,
                    "car_value": 0 if sp.get("is_parked") else 1,
                    "car_time": now_str,
                    "pay_info": None,
                    "source": "taitung",
                }
                results.append(sanitize(rec))
            except Exception:
                pass
    except Exception:
        pass

    return results


# 9. 南投縣 (nantou)
def fetch_nantou() -> List[Dict[str, Any]]:
    url = "https://parking.nantou.gov.tw/ParkingLocation/ParkingLotPost"
    raw = fetch_url(url, method="POST", data=b"", timeout=20)
    data = json.loads(raw.decode("utf-8"))
    results = []
    for item in data:
        name = (item.get("carParkName") or "").strip()
        lat = item.get("positionLat")
        lon = item.get("positionLon")
        if not name or lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (ValueError, TypeError):
            continue
        fare = item.get("fareDescription") or item.get("description")
        pay_info = str(fare).strip() if fare else None
        rec = {
            "name": name,
            "lat": lat_f,
            "lon": lon_f,
            "car_total": None,
            "car_value": None,
            "car_time": None,
            "pay_info": pay_info,
            "source": "nantou",
        }
        results.append(sanitize(rec))
    return results


# 10. 澎湖縣 (penghu)
def fetch_penghu() -> List[Dict[str, Any]]:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lots_config = [
        {"code": "OWIZXZ", "name": "澎湖縣府地下停車場", "lat": 23.569694, "lon": 119.566454, "total": 68},
        {"code": "QAHZCZ", "name": "馬公國小地下停車場", "lat": 23.571100, "lon": 119.569200, "total": 516},
        {"code": "T2THK8", "name": "中正國小地下停車場", "lat": 23.567961, "lon": 119.565528, "total": 105},
        {"code": "NCEJ97", "name": "中興國小地下停車場", "lat": 23.574800, "lon": 119.574800, "total": 347},
        {"code": "Y7ROW9", "name": "文光國中地下停車場", "lat": 23.573167, "lon": 119.570075, "total": 125},
    ]

    results = []
    for cfg in lots_config:
        url = f"https://zytparking.com:35170/api/external-setting/parking-lot-available-space?parkingLotCode={cfg['code']}"
        raw = fetch_url(url, timeout=5)
        data = json.loads(raw.decode("utf-8"))
        avail = data.get("available")
        car_val = int(str(avail).strip()) if avail is not None else None

        rec = {
            "name": cfg["name"],
            "lat": cfg["lat"],
            "lon": cfg["lon"],
            "car_total": cfg["total"],
            "car_value": car_val,
            "car_time": now_str if car_val is not None else None,
            "pay_info": "每小時20元，當日上限80元",
            "source": "penghu",
        }
        results.append(sanitize(rec))
    return results


# 查詢時實際會跑的來源。
# 屏東縣路外／嘉義縣／新竹縣三支**不在這裡**——那三份原始資料沒有座標，
# 現抓也回 0 筆、只是白白多打三個 HTTP 請求；它們改由 `static_store` 這一格供應，
# 資料由 `build-static-store.py` 事先抓好並轉成座標存在本地（零 API 呼叫）。
# 它們的線上抓取函式（fetch_pthg_offstreet／fetch_cyhg／fetch_hsinchu）保留備查，只是不掛進來。
ADAPTERS = {
    "static_store": fetch_static_store,
    "pthg_hengchun": fetch_pthg_hengchun,
    "chcg": fetch_chcg,
    "yunlin": fetch_yunlin,
    "hualien": fetch_hualien,
    "taitung": fetch_taitung,
    "nantou": fetch_nantou,
    "penghu": fetch_penghu,
}
