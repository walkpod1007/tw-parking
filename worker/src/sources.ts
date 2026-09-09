import { Env } from "./types";
import { sanitize, haversineDistance } from "./parking";
import store from "../static-store.json";

function getTaipeiNowString(nowMs: number = Date.now()): string {
  const d = new Date(nowMs + 8 * 3600 * 1000);
  const iso = d.toISOString();
  return iso.slice(0, 10) + " " + iso.slice(11, 19);
}

function unescapeHtml(s: string): string {
  return s
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ");
}

function parseCsvSimple(text: string): Record<string, string>[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (lines.length < 2) return [];
  const header = lines[0].split(",").map((h) => h.trim().replace(/^["']|["']$/g, ""));
  const rows: Record<string, string>[] = [];
  for (let i = 1; i < lines.length; i++) {
    const parts = lines[i].split(",").map((p) => p.trim().replace(/^["']|["']$/g, ""));
    const row: Record<string, string> = {};
    for (let j = 0; j < header.length; j++) {
      row[header[j]] = parts[j] ?? "";
    }
    rows.push(row);
  }
  return rows;
}

async function sha256Hex(data: string): Promise<string> {
  const buf = new TextEncoder().encode(data);
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

const inFlightRequests = new Map<string, Promise<Response>>();

export async function fetchWithCache(
  url: string,
  options: {
    method?: string;
    body?: string;
    headers?: Record<string, string>;
    timeoutMs?: number;
    ttlOverride?: number;
  } = {},
  env?: Env,
  ctx?: ExecutionContext
): Promise<string> {
  const method = options.method || "GET";
  const body = options.body || "";
  const timeoutMs = options.timeoutMs ?? 8000;
  const ttl = options.ttlOverride !== undefined
    ? options.ttlOverride
    : parseInt(env?.TW_PARKING_HTTP_TTL ?? "60", 10);

  let cache: Cache | null = null;
  let cacheKeyUrl: string | null = null;

  if (ttl > 0) {
    try {
      cache = caches.default;
      const hash = await sha256Hex(`${method} ${url} ${body}`);
      cacheKeyUrl = `https://cache.tw-parking.local/${hash}`;
      const cached = await cache.match(cacheKeyUrl);
      if (cached) {
        return await cached.text();
      }
    } catch {
      // cache match failed or not available in test
    }
  }

  const flightKey = `${method} ${url} ${body}`;
  let resp: Response;

  const existingFlight = inFlightRequests.get(flightKey);
  if (existingFlight) {
    const origResp = await existingFlight;
    resp = origResp.clone();
  } else {
    const headers: Record<string, string> = {
      "User-Agent": "Mozilla/5.0",
      ...(options.headers || {})
    };
    const flightPromise = fetch(url, {
      method,
      headers,
      body: method !== "GET" && body ? body : undefined,
      signal: AbortSignal.timeout(timeoutMs)
    });
    inFlightRequests.set(flightKey, flightPromise);
    try {
      const origResp = await flightPromise;
      resp = origResp.clone();
    } finally {
      inFlightRequests.delete(flightKey);
    }
  }

  if (!resp.ok) {
    throw new Error(`HTTP error: ${resp.status} ${resp.statusText}`);
  }

  const text = await resp.text();
  const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
  if (text.length > MAX_RESPONSE_BYTES) {
    throw new Error(`上游回應超過 ${MAX_RESPONSE_BYTES} bytes：${url}`);
  }

  if (cache && cacheKeyUrl && ttl > 0) {
    try {
      const cacheResp = new Response(text, {
        status: 200,
        headers: {
          "Content-Type": resp.headers.get("Content-Type") || "text/plain; charset=utf-8",
          "Cache-Control": `max-age=${ttl}`
        }
      });
      if (ctx?.waitUntil) {
        ctx.waitUntil(cache.put(cacheKeyUrl, cacheResp));
      } else {
        await cache.put(cacheKeyUrl, cacheResp);
      }
    } catch {
      // ignore cache write errors
    }
  }

  return text;
}

// Static Store records prepared once at module initialization
const STATIC_RECORDS: Record<string, any>[] = ((store as any).records || [])
  .filter((r: any) => r.lat !== null && r.lat !== undefined && r.lon !== null && r.lon !== undefined)
  .map((r: any) => {
    const rec = sanitize({
      name: r.name,
      lat: Number(r.lat),
      lon: Number(r.lon),
      car_total: r.car_total ?? null,
      car_value: null,
      car_time: null,
      pay_info: r.pay_info ?? null,
      source: r.source || "static"
    });
    rec.geo_precision = r.geo_precision || "unknown";
    return rec;
  });

export function fetchStaticStore(): Record<string, any>[] {
  return STATIC_RECORDS.map((r) => ({ ...r }));
}

export async function fetchParkingSpaces(
  lat: number,
  lon: number,
  env?: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  const baseUrl = (env?.TW_PARKING_PARKBOSS_URL || "https://parkboss.tw").replace(/\/+$/, "");
  const url = `${baseUrl}/api/v1/query-parking-space-by-coordinate?lat=${lat}&lon=${lon}`;
  const raw = await fetchWithCache(url, { timeoutMs: 8000 }, env, ctx);
  let data: any;
  try {
    data = JSON.parse(raw);
  } catch (err: any) {
    throw new Error(`JSON parse error: ${err.message}`);
  }
  if (!Array.isArray(data)) {
    throw new Error(`Unexpected response format: expected list`);
  }
  for (const item of data) {
    if (!item.source) {
      item.source = "parkboss";
    }
  }
  return data;
}

// 1. 屏東恆春
export async function fetchPthgHengchun(env?: Env, ctx?: ExecutionContext): Promise<Record<string, any>[]> {
  const url = "https://www-ws.pthg.gov.tw/Upload/2015pthg/0/relfile/0/0/0a83a598-d9a2-4a00-814c-9c255ea02ae9.csv";
  const raw = await fetchWithCache(url, { timeoutMs: 8000 }, env, ctx);
  const rows = parseCsvSimple(raw);
  const results: Record<string, any>[] = [];
  for (const row of rows) {
    const name = (row["parkingLotName"] || "").trim();
    const lat = row["entranceLatitude"] || row["exitLatitude"];
    const lon = row["entranceLongitude"] || row["exitLongitude"];
    if (!name || !lat || !lon) continue;
    const latF = parseFloat(lat);
    const lonF = parseFloat(lon);
    if (isNaN(latF) || isNaN(lonF)) continue;
    const rec = {
      name,
      lat: latF,
      lon: lonF,
      car_total: null,
      car_value: null,
      car_time: null,
      pay_info: null,
      source: "pthg_hengchun"
    };
    results.push(sanitize(rec));
  }
  return results;
}

// 2. 彰化縣
export async function fetchChcg(env?: Env, ctx?: ExecutionContext): Promise<Record<string, any>[]> {
  const url = "https://chpark.chcg.gov.tw/ParkingLocation/ParkingLotPost";
  const raw = await fetchWithCache(url, { method: "POST", body: "", timeoutMs: 8000 }, env, ctx);
  const data = JSON.parse(raw);
  const nowStr = getTaipeiNowString();
  const results: Record<string, any>[] = [];
  for (const item of data) {
    const name = (item.carParkName || "").trim();
    const lat = item.positionLat;
    const lon = item.positionLon;
    if (!name || lat === null || lat === undefined || lon === null || lon === undefined) continue;
    const latF = parseFloat(lat);
    const lonF = parseFloat(lon);
    if (isNaN(latF) || isNaN(lonF)) continue;

    let carTotal: number | null = null;
    const totalRaw = item.totalNum;
    if (totalRaw !== null && totalRaw !== undefined && totalRaw !== "" && totalRaw !== "-") {
      const parsed = parseInt(String(totalRaw).trim(), 10);
      if (!isNaN(parsed)) carTotal = parsed;
    }

    let carValue: number | null = null;
    let carTime: string | null = null;
    const remRaw = item.remaining;
    if (remRaw !== null && remRaw !== undefined && remRaw !== "" && remRaw !== "-") {
      const parsed = parseInt(String(remRaw).trim(), 10);
      if (!isNaN(parsed)) {
        carValue = parsed;
        carTime = nowStr;
      }
    }

    const fare = item.fareDescription || item.description;
    const payInfo = fare ? String(fare).trim() : null;

    const rec = {
      name,
      lat: latF,
      lon: lonF,
      car_total: carTotal,
      car_value: carValue,
      car_time: carTime,
      pay_info: payInfo,
      source: "chcg"
    };
    results.push(sanitize(rec));
  }
  return results;
}

// 3. 雲林縣
export async function fetchYunlin(env?: Env, ctx?: ExecutionContext): Promise<Record<string, any>[]> {
  const url = "https://parking.yunlin.gov.tw/ParkingLocation/ParkingLotPost";
  const raw = await fetchWithCache(url, { method: "POST", body: "", timeoutMs: 8000 }, env, ctx);
  const data = JSON.parse(raw);
  const results: Record<string, any>[] = [];
  for (const item of data) {
    const name = (item.carParkName || "").trim();
    const lat = item.positionLat;
    const lon = item.positionLon;
    if (!name || lat === null || lat === undefined || lon === null || lon === undefined) continue;
    const latF = parseFloat(lat);
    const lonF = parseFloat(lon);
    if (isNaN(latF) || isNaN(lonF)) continue;

    const fare = item.fareDescription || item.description;
    const payInfo = fare ? String(fare).trim() : null;

    const rec = {
      name,
      lat: latF,
      lon: lonF,
      car_total: null,
      car_value: null,
      car_time: null,
      pay_info: payInfo,
      source: "yunlin"
    };
    results.push(sanitize(rec));
  }
  return results;
}

// 4. 花蓮縣
export async function fetchHualien(env?: Env, ctx?: ExecutionContext): Promise<Record<string, any>[]> {
  const url = "https://traffic.hl.gov.tw/Home/_ParkingDetailPartialView";
  const body = "page=1&pageNumber=100&action=DynamicParking&currentGroup=1&dataModel[KeyWord]=";
  const raw = await fetchWithCache(
    url,
    {
      method: "POST",
      body,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      timeoutMs: 8000
    },
    env,
    ctx
  );

  const results: Record<string, any>[] = [];
  const items = raw.split("parking_space_list_item");
  for (const block of items.slice(1)) {
    const mName = block.match(/class="parking_space_item_title"[^>]*>[\s\S]*?<a [^>]*>([^<]+)<\/a>/);
    const name = mName ? unescapeHtml(mName[1].trim()) : null;

    const mCoords = block.match(/q=([0-9.-]+),([0-9.-]+)/);
    if (!mCoords || !name) continue;
    const latF = parseFloat(mCoords[1]);
    const lonF = parseFloat(mCoords[2]);
    if (isNaN(latF) || isNaN(lonF)) continue;

    const mTot = block.match(/總停車位：<\/div>\s*<div class="text_content">(\d+)<\/div>/);
    const carTotal = mTot ? parseInt(mTot[1], 10) : null;

    const mFee = block.match(/收費方式：<\/div>\s*<div class="text_content">([^<]+)<\/div>/);
    const payInfo = mFee ? unescapeHtml(mFee[1].trim()) : null;

    const rec = {
      name,
      lat: latF,
      lon: lonF,
      car_total: carTotal,
      car_value: null,
      car_time: null,
      pay_info: payInfo,
      source: "hualien"
    };
    results.push(sanitize(rec));
  }
  return results;
}

// 5. 台東縣
export function pickTaitungCandidates(
  lots: any[],
  queryLat: number,
  queryLon: number,
  radiusM: number
): any[] {
  const withDist: { item: any; dist: number }[] = [];

  for (const item of lots) {
    const lat = Number(item.PositionLat ?? item.lat);
    const lon = Number(item.PositionLon ?? item.lng ?? item.lon);
    if (isNaN(lat) || isNaN(lon)) continue;

    const dist = haversineDistance(queryLat, queryLon, lat, lon);
    if (dist <= radiusM) {
      withDist.push({ item, dist });
    }
  }

  withDist.sort((a, b) => a.dist - b.dist);

  if (withDist.length > 12) {
    console.log(`taitung truncated ${withDist.length} -> 12`);
    return withDist.slice(0, 12).map((x) => x.item);
  }

  return withDist.map((x) => x.item);
}

export async function fetchTaitung(
  queryLat: number,
  queryLon: number,
  radiusM: number = 0,
  env?: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  const nowStr = getTaipeiNowString();
  const results: Record<string, any>[] = [];

  // 5.1 路外停車場
  const urlLots = "https://trafficweb.ttcpb.gov.tw/api/parking-lots";
  try {
    const rawLots = await fetchWithCache(urlLots, { timeoutMs: 8000 }, env, ctx);
    const lotsData: any[] = JSON.parse(rawLots).data || [];

    const candidateLots = pickTaitungCandidates(lotsData, queryLat, queryLon, radiusM);

    const detailTasks = candidateLots.map(async (item) => {
      const lid = item.id;
      if (!lid) return null;
      try {
        const rawD = await fetchWithCache(`https://trafficweb.ttcpb.gov.tw/api/parking-lots/${lid}`, { timeoutMs: 5000 }, env, ctx);
        const d = JSON.parse(rawD).data;
        if (!d) return null;
        const name = d.name || `臺東停車場-${lid}`;
        const latF = parseFloat(d.lat);
        const lonF = parseFloat(d.lng);
        if (isNaN(latF) || isNaN(lonF)) return null;

        const total = d.total !== null && d.total !== undefined ? parseInt(String(d.total), 10) : null;
        const vacancy = d.vacancy !== null && d.vacancy !== undefined ? parseInt(String(d.vacancy), 10) : null;

        const rec = {
          name,
          lat: latF,
          lon: lonF,
          car_total: total,
          car_value: vacancy,
          car_time: vacancy !== null ? nowStr : null,
          pay_info: d.charge || null,
          source: "taitung"
        };
        return sanitize(rec);
      } catch {
        return null;
      }
    });

    const lotResults = await Promise.all(detailTasks);
    for (const r of lotResults) {
      if (r) results.push(r);
    }
  } catch {
    // ignore
  }

  // 5.2 智慧路邊停車格
  const urlSpaces = "https://trafficweb.ttcpb.gov.tw/api/parking-spaces";
  try {
    const rawSpaces = await fetchWithCache(urlSpaces, { timeoutMs: 8000 }, env, ctx);
    const spacesData: any[] = JSON.parse(rawSpaces).data || [];
    for (const sp of spacesData) {
      try {
        const latF = parseFloat(sp.lat);
        const lonF = parseFloat(sp.lng);
        if (isNaN(latF) || isNaN(lonF)) continue;
        const rec = {
          name: `臺東智慧車格-${sp.id}`,
          lat: latF,
          lon: lonF,
          car_total: 1,
          car_value: sp.is_parked ? 0 : 1,
          car_time: nowStr,
          pay_info: null,
          source: "taitung"
        };
        results.push(sanitize(rec));
      } catch {
        // ignore single space error
      }
    }
  } catch {
    // ignore
  }

  return results;
}

// 6. 南投縣
export async function fetchNantou(env?: Env, ctx?: ExecutionContext): Promise<Record<string, any>[]> {
  const url = "https://parking.nantou.gov.tw/ParkingLocation/ParkingLotPost";
  const raw = await fetchWithCache(url, { method: "POST", body: "", timeoutMs: 8000 }, env, ctx);
  const data = JSON.parse(raw);
  const results: Record<string, any>[] = [];
  for (const item of data) {
    const name = (item.carParkName || "").trim();
    const lat = item.positionLat;
    const lon = item.positionLon;
    if (!name || lat === null || lat === undefined || lon === null || lon === undefined) continue;
    const latF = parseFloat(lat);
    const lonF = parseFloat(lon);
    if (isNaN(latF) || isNaN(lonF)) continue;

    const fare = item.fareDescription || item.description;
    const payInfo = fare ? String(fare).trim() : null;

    const rec = {
      name,
      lat: latF,
      lon: lonF,
      car_total: null,
      car_value: null,
      car_time: null,
      pay_info: payInfo,
      source: "nantou"
    };
    results.push(sanitize(rec));
  }
  return results;
}

// 7. 澎湖縣
export async function fetchPenghu(env?: Env, ctx?: ExecutionContext): Promise<Record<string, any>[]> {
  const nowStr = getTaipeiNowString();
  const lotsConfig = [
    { code: "OWIZXZ", name: "澎湖縣府地下停車場", lat: 23.569694, lon: 119.566454, total: 68 },
    { code: "QAHZCZ", name: "馬公國小地下停車場", lat: 23.571100, lon: 119.569200, total: 516 },
    { code: "T2THK8", name: "中正國小地下停車場", lat: 23.567961, lon: 119.565528, total: 105 },
    { code: "NCEJ97", name: "中興國小地下停車場", lat: 23.574800, lon: 119.574800, total: 347 },
    { code: "Y7ROW9", name: "文光國中地下停車場", lat: 23.573167, lon: 119.570075, total: 125 }
  ];

  const tasks = lotsConfig.map(async (cfg) => {
    try {
      const url = `https://zytparking.com:35170/api/external-setting/parking-lot-available-space?parkingLotCode=${cfg.code}`;
      const raw = await fetchWithCache(url, { timeoutMs: 5000 }, env, ctx);
      const data = JSON.parse(raw);
      const avail = data.available;
      const carVal = avail !== null && avail !== undefined ? parseInt(String(avail).trim(), 10) : null;
      const rec = {
        name: cfg.name,
        lat: cfg.lat,
        lon: cfg.lon,
        car_total: cfg.total,
        car_value: carVal,
        car_time: carVal !== null ? nowStr : null,
        pay_info: "每小時20元，當日上限80元",
        source: "penghu"
      };
      return sanitize(rec);
    } catch {
      return null;
    }
  });

  const resolved = await Promise.all(tasks);
  return resolved.filter((r): r is Record<string, any> => r !== null);
}

export const ADAPTER_BOXES: Record<string, [number, number, number, number]> = {
  pthg_hengchun: [21.85, 22.20, 120.65, 120.95],
  chcg:          [23.70, 24.22, 120.22, 120.78],
  yunlin:        [23.42, 23.90, 120.08, 120.75],
  hualien:       [22.95, 24.40, 121.05, 121.90],
  taitung:       [22.25, 23.50, 120.72, 121.68],
  nantou:        [23.40, 24.28, 120.58, 121.38],
  penghu:        [23.15, 23.85, 119.28, 119.75]
};

const BOX_MARGIN_DEG = 0.15;

export function adaptersFor(lat: number, lon: number, radiusM: number = 0.0): string[] {
  const margin = BOX_MARGIN_DEG + (radiusM || 0.0) / 111000.0;
  const picked: string[] = ["static_store"];
  for (const [slug, box] of Object.entries(ADAPTER_BOXES)) {
    const [latMin, latMax, lonMin, lonMax] = box;
    if (
      latMin - margin <= lat &&
      lat <= latMax + margin &&
      lonMin - margin <= lon &&
      lon <= lonMax + margin
    ) {
      picked.push(slug);
    }
  }
  return picked;
}

export async function fetchAdapter(
  slug: string,
  lat: number,
  lon: number,
  radiusM: number,
  env?: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  switch (slug) {
    case "static_store":
      return fetchStaticStore();
    case "pthg_hengchun":
      return fetchPthgHengchun(env, ctx);
    case "chcg":
      return fetchChcg(env, ctx);
    case "yunlin":
      return fetchYunlin(env, ctx);
    case "hualien":
      return fetchHualien(env, ctx);
    case "taitung":
      return fetchTaitung(lat, lon, radiusM, env, ctx);
    case "nantou":
      return fetchNantou(env, ctx);
    case "penghu":
      return fetchPenghu(env, ctx);
    default:
      return [];
  }
}

