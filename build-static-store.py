#!/usr/bin/env python3
"""build-static-store.py — 把「只有地址沒有經緯度」的縣市停車場資料一次抓下來、
地址批次轉座標，存成本地一份靜態庫，之後查詢零 API 呼叫。

為什麼有這支：
屏東縣路外、嘉義縣、新竹縣三份政府開放資料**只給地址不給經緯度**（實測欄位確認），
距離查詢完全用不到，接進 sources.py 之後那三支恆回 0 筆。
場名、座標、格數、費率一年變不了幾次，沒有理由每次查詢都現抓現轉。

設計：
- 抓 → 轉座標 → 寫 `static-store.json`（純政府公開資料，可進版控，**不含任何金鑰**）
- 轉座標走 PARKING_GEOCODE_CMD 指定的指令（例如自己的 gmaps wrapper），**逐筆快取在同一份檔案裡**：
  已經有座標的地址不再打第二次，所以重跑一次只花新增筆數的錢。
- 排程建議每週一趟即可。

用法：
  python3 build-static-store.py            # 增量：只轉還沒有座標的
  python3 build-static-store.py --stats    # 只看現況，不抓不轉
  python3 build-static-store.py --limit 50 # 這趟最多轉 50 筆（控制花費）
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources  # noqa: E402

STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static-store.json")
GMAPS = os.environ.get("PARKING_GEOCODE_CMD", "").strip()  # 例：/path/to/gmaps；空＝不做地理編碼


def load_store():
    if not os.path.exists(STORE):
        return {"version": 1, "updated": None, "records": []}
    with open(STORE, encoding="utf-8") as f:
        return json.load(f)


def save_store(store):
    store["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)


def geocode(address):
    """回 (lat, lon, precision)；查不到回 None。

    precision 有兩種值，**這一欄比座標本身更重要**：
      "exact"  — 地址真的被解析到門牌
      "approx" — Google 回 partial_match，意思是「你給的地址我認不出來，
                 這是我猜的最接近的東西」。台灣的停車場開放資料大量用**地號**
                 （「竹北市站後段8、9、10…等13筆地號」），地號查不到門牌，
                 Google 會退回鄉鎮市中心點——2026-09-08 實測新竹縣 136 筆裡
                 有 66 筆全部落在竹北市同一個點，看起來每一場都在你隔壁。
                 假座標比查無更會害人，所以要標出來讓下游講清楚。
    """
    try:
        out = subprocess.run([GMAPS, "geocode", "--json", address], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    except Exception:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return None
    top = results[0]
    loc = top.get("geometry", {}).get("location", {})
    lat, lon = loc.get("lat"), loc.get("lng")
    if lat is None or lon is None:
        return None
    # partial_match 為真＝這筆是猜的；location_type 不可靠（實測地號被配到
    # 一間汽車修理廠仍回 ROOFTOP），只有 partial_match 認得出來。
    precision = "approx" if top.get("partial_match") else "exact"
    return float(lat), float(lon), precision


# 只有地址沒有座標的來源；每支回 [{name, address, car_total, pay_info, source}]
ADDRESS_ONLY = {
    "pthg": sources.fetch_pthg_offstreet_raw,
    "cyhg": sources.fetch_cyhg_raw,
    "hsinchu": sources.fetch_hsinchu_raw,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="這趟最多轉幾筆座標（0＝不限）")
    args = ap.parse_args()

    store = load_store()
    by_key = {(r["source"], r["name"], r.get("address", "")): r for r in store["records"]}

    if args.stats:
        have = sum(1 for r in store["records"] if r.get("lat") is not None)
        print(f"靜態庫 {len(store['records'])} 筆，其中 {have} 筆有座標；最後更新 {store.get('updated')}")
        for s in sorted({r['source'] for r in store['records']}):
            rows = [r for r in store["records"] if r["source"] == s]
            print(f"  {s:9s} {len(rows):4d} 筆｜有座標 {sum(1 for r in rows if r.get('lat') is not None)}")
        return 0

    fetched = 0
    for name, fn in ADDRESS_ONLY.items():
        try:
            rows = fn()
        except Exception as e:
            sys.stderr.write(f"[warn] {name} 抓不到：{e}\n")
            continue
        fetched += len(rows)
        for row in rows:
            key = (row["source"], row["name"], row.get("address", ""))
            if key in by_key:
                # 保留既有座標，其餘欄位以這次抓到的為準（費率格數會變）
                old = by_key[key]
                row["lat"], row["lon"] = old.get("lat"), old.get("lon")
                by_key[key] = row
            else:
                row["lat"] = row["lon"] = None
                by_key[key] = row

    records = list(by_key.values())
    todo = [r for r in records if r.get("lat") is None and r.get("address")]
    if args.limit:
        todo = todo[: args.limit]

    print(f"抓到 {fetched} 筆；庫內共 {len(records)} 筆；這趟要轉座標 {len(todo)} 筆")
    done = fail = 0
    for i, r in enumerate(todo, 1):
        got = geocode(r["address"])
        if got:
            r["lat"], r["lon"], r["geo_precision"] = got
            done += 1
        else:
            fail += 1
        if i % 25 == 0:
            print(f"  …{i}/{len(todo)}（成功 {done}／失敗 {fail}）")
            store["records"] = records
            save_store(store)  # 中途也存，撞限額或被中斷不會整趟白做

    store["records"] = records
    save_store(store)
    have = sum(1 for r in records if r.get("lat") is not None)
    print(f"完成：新轉 {done} 筆、轉不出 {fail} 筆；庫內有座標 {have}/{len(records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
