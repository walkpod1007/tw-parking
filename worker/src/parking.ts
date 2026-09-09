import { Env, ParkingRecord } from "./types";
import { adaptersFor, fetchAdapter, fetchParkingSpaces } from "./sources";
import { fetchTdx, availabilityOverlay } from "./tdx";

export function haversineDistance(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371000.0;
  const phi1 = (lat1 * Math.PI) / 180.0;
  const phi2 = (lat2 * Math.PI) / 180.0;
  const deltaPhi = ((lat2 - lat1) * Math.PI) / 180.0;
  const deltaLambda = ((lon2 - lon1) * Math.PI) / 180.0;

  const a =
    Math.sin(deltaPhi / 2.0) ** 2 +
    Math.cos(phi1) * Math.cos(phi2) * Math.sin(deltaLambda / 2.0) ** 2;
  const c = 2.0 * Math.atan2(Math.sqrt(a), Math.sqrt(1.0 - a));
  return R * c;
}

export function extractRateSummary(payInfo?: string | null): string {
  if (!payInfo || typeof payInfo !== "string") {
    return "";
  }
  const clean = payInfo.replace(/\r/g, " ").replace(/\n/g, " ").trim();

  const halfPatterns = [/每半小時(?:收費)?(\d+)元/, /(\d+)元\s*\/\s*半小時/];
  const hourPatterns = [/每小時(?:收費)?(\d+)元/, /(\d+)元\s*\/\s*(?:小)?時/];

  for (const pat of halfPatterns) {
    const m = clean.match(pat);
    if (m) {
      return `每半小時 ${m[1]} 元`;
    }
  }
  for (const pat of hourPatterns) {
    const m = clean.match(pat);
    if (m) {
      return `每小時 ${m[1]} 元`;
    }
  }
  if (clean.length > 16) {
    return clean.slice(0, 16) + "…";
  }
  return clean;
}

export function parseCarTime(timeStr?: string | null): number | null {
  if (!timeStr || typeof timeStr !== "string") {
    return null;
  }
  const s = timeStr.trim();
  if (!s) return null;

  const match = s.match(
    /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?(?:\s*(Z|[+-]\d{2}(?::?\d{2})?))?$/
  );
  if (!match) {
    return null;
  }

  const year = parseInt(match[1], 10);
  const month = parseInt(match[2], 10);
  const day = parseInt(match[3], 10);
  if (month < 1 || month > 12 || day < 1 || day > 31) {
    return null;
  }

  const hour = match[4] !== undefined ? parseInt(match[4], 10) : 0;
  const minute = match[5] !== undefined ? parseInt(match[5], 10) : 0;
  const second = match[6] !== undefined ? parseInt(match[6], 10) : 0;
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 || second > 59) {
    return null;
  }

  let ms = 0;
  if (match[7]) {
    const frac = match[7].padEnd(3, "0").slice(0, 3);
    ms = parseInt(frac, 10);
  }

  const utcMs = Date.UTC(year, month - 1, day, hour, minute, second, ms);
  const check = new Date(utcMs);
  if (
    check.getUTCFullYear() !== year ||
    check.getUTCMonth() !== month - 1 ||
    check.getUTCDate() !== day ||
    check.getUTCHours() !== hour ||
    check.getUTCMinutes() !== minute ||
    check.getUTCSeconds() !== second
  ) {
    return null;
  }

  const tz = match[8];
  if (tz) {
    let offsetMinutes = 0;
    if (tz === "Z" || tz === "z") {
      offsetMinutes = 0;
    } else {
      const tzMatch = tz.match(/^([+-])(\d{2}):?(\d{2})?$/);
      if (!tzMatch) return null;
      const sign = tzMatch[1] === "+" ? 1 : -1;
      const tzH = parseInt(tzMatch[2], 10);
      const tzM = tzMatch[3] ? parseInt(tzMatch[3], 10) : 0;
      offsetMinutes = sign * (tzH * 60 + tzM);
    }
    return utcMs - offsetMinutes * 60 * 1000;
  } else {
    return utcMs - 8 * 3600 * 1000;
  }
}

export function calculateFreshness(
  carTimeStr?: string | null,
  nowMs?: number
): { fresh_level: "live" | "recent" | "expired" | "never"; fresh_text: string; is_expired: boolean } {
  const now = nowMs ?? Date.now();

  if (!carTimeStr || !carTimeStr.trim()) {
    return {
      fresh_level: "never",
      fresh_text: "無即時回報",
      is_expired: true
    };
  }

  const carMs = parseCarTime(carTimeStr);
  if (carMs === null) {
    return {
      fresh_level: "expired",
      fresh_text: `數字過期（最後更新 ${carTimeStr.trim().slice(0, 10)}）`,
      is_expired: true
    };
  }

  const diffSeconds = (now - carMs) / 1000;
  if (diffSeconds <= 1800) {
    return {
      fresh_level: "live",
      fresh_text: "即時",
      is_expired: false
    };
  } else if (diffSeconds <= 86400) {
    const hours = Math.max(1, Math.floor(diffSeconds / 3600));
    return {
      fresh_level: "recent",
      fresh_text: `${hours} 小時前`,
      is_expired: false
    };
  } else {
    const d = new Date(carMs + 8 * 3600 * 1000);
    const dateStr = d.toISOString().slice(0, 10);
    return {
      fresh_level: "expired",
      fresh_text: `數字過期（最後更新 ${dateStr}）`,
      is_expired: true
    };
  }
}

export const AVAIL_LABELS: Record<string, [string, string, string]> = {
  unknown: ["❓", "空位不明", "unknown"],
  full:    ["🔴", "已滿", "full"],
  low:     ["🟡", "快滿", "low"],
  green:   ["🟢", "有位", "available"]
};

export function toStrictInt(value: any): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") {
    return Number.isInteger(value) ? value : null;
  }
  if (typeof value === "string") {
    const s = value.trim();
    if (/^-?\d+$/.test(s)) {
      const n = Number(s);
      return Number.isSafeInteger(n) ? n : null;
    }
  }
  return null;
}

export function availabilityKey(value: any, expired: boolean): "unknown" | "full" | "low" | "green" {
  if (expired || value === null || value === undefined || value === "") {
    return "unknown";
  }
  const v = toStrictInt(value);
  if (v === null) {
    return "unknown";
  }
  if (v === 0) {
    return "full";
  }
  if (v <= 2) {
    return "low";
  }
  return "green";
}

export function isSameParkingLot(rec1: Record<string, any>, rec2: Record<string, any>): boolean {
  if (rec1.source && rec2.source && rec1.source === rec2.source) {
    return false;
  }
  const lat1 = Number(rec1.lat);
  const lon1 = Number(rec1.lon);
  const lat2 = Number(rec2.lat);
  const lon2 = Number(rec2.lon);
  if (!Number.isFinite(lat1) || !Number.isFinite(lon1) || !Number.isFinite(lat2) || !Number.isFinite(lon2)) {
    return false;
  }
  if (haversineDistance(lat1, lon1, lat2, lon2) >= 50.0) {
    return false;
  }
  const name1 = String(rec1.name || "").replace(/\s+/g, "");
  const name2 = String(rec2.name || "").replace(/\s+/g, "");
  if (!name1 || !name2) {
    return false;
  }
  return name1.includes(name2) || name2.includes(name1);
}

export function shouldReplace(newRec: Record<string, any>, oldRec: Record<string, any>): boolean {
  const newHas = newRec.car_value !== null && newRec.car_value !== undefined;
  const oldHas = oldRec.car_value !== null && oldRec.car_value !== undefined;

  if (newHas && !oldHas) return true;
  if (oldHas && !newHas) return false;

  if (newRec.source === "parkboss" && oldRec.source !== "parkboss") {
    return true;
  }
  return false;
}

export function deduplicateRecords(records: Record<string, any>[]): Record<string, any>[] {
  const unique: Record<string, any>[] = [];
  for (const rec of records) {
    let matchIdx = -1;
    for (let i = 0; i < unique.length; i++) {
      if (isSameParkingLot(rec, unique[i])) {
        matchIdx = i;
        break;
      }
    }
    if (matchIdx === -1) {
      unique.push(rec);
    } else {
      if (shouldReplace(rec, unique[matchIdx])) {
        unique[matchIdx] = rec;
      }
    }
  }
  return unique;
}

const GENERIC_LOT_CORES = new Set([
  "第一", "第二", "第三", "第四", "第五",
  "站前", "公有", "臨時", "收費", "免費", "公用",
  "體育館", "公園", "市場", "廣場", "漁港", "河濱",
  "火車站", "轉運站", "衛生所", "鄉公所", "鎮公所", "區公所", "圖書館"
]);

function norm(x: any): string {
  return String(x || "").replace(/\s+/g, "");
}

function core(name: string): string {
  return name.replace(/(立體|地下|平面)?停車(場|位)$/, "");
}

export function applyAvailabilityOverlay(
  records: Record<string, any>[],
  overlay: Record<string, any>[]
): void {
  for (const rec of records) {
    if (rec.car_value !== null && rec.car_value !== undefined) {
      continue;
    }
    const rname = norm(rec.name);
    if (rname.length < 4) continue;
    const rlat = rec.lat;
    const rlon = rec.lon;

    for (const a of overlay) {
      const aname = norm(a.name);
      if (aname.length < 4) continue;

      if (rname === aname) {
        // exact match
      } else if (rname.includes(aname) || aname.includes(rname)) {
        const shorter = rname.length <= aname.length ? rname : aname;
        if (GENERIC_LOT_CORES.has(core(shorter)) || shorter.length < 6) {
          continue;
        }
      } else {
        continue;
      }

      const box = a.box;
      if (box && rlat !== null && rlat !== undefined && rlon !== null && rlon !== undefined) {
        const [la0, la1, lo0, lo1] = box;
        const latNum = Number(rlat);
        const lonNum = Number(rlon);
        if (!(la0 <= latNum && latNum <= la1 && lo0 <= lonNum && lonNum <= lo1)) {
          continue;
        }
      }

      rec.car_value = a.car_value;
      if (rec.car_total === null || rec.car_total === undefined || rec.car_total === 0) {
        rec.car_total = a.car_total;
      }
      rec.car_time = a.car_time;
      break;
    }
  }
}

export function sanitize(rec: Record<string, any>): Record<string, any> {
  const carVal = rec.car_value;
  const carTot = rec.car_total;

  if (carVal !== null && carVal !== undefined) {
    const numVal = toStrictInt(carVal);
    const numTot = carTot !== null && carTot !== undefined ? toStrictInt(carTot) : null;
    if (numVal !== null) {
      if (numVal < 0) {
        rec.car_value = null;
        rec.dirty_reason = `剩餘車位為負數 (${carVal})`;
      } else if (numTot !== null && numVal > numTot) {
        rec.car_value = null;
        rec.dirty_reason = `剩餘車位 (${carVal}) 大於總格數 (${carTot})`;
      }
    }
  }

  if (carTot !== null && carTot !== undefined) {
    const numTot = toStrictInt(carTot);
    if (numTot !== null && numTot <= 0) {
      rec.car_total = null;
    }
  }

  return rec;
}

export function processRecords(
  data: Record<string, any>[],
  centerLat: number,
  centerLon: number,
  radius: number,
  includeMoto: boolean = false,
  minTotal: number = 6,
  nowMs?: number
): Record<string, any>[] {
  const results: Record<string, any>[] = [];

  for (const item of data) {
    if (item.lat === null || item.lat === undefined || item.lon === null || item.lon === undefined) {
      continue;
    }
    const latNum = Number(item.lat);
    const lonNum = Number(item.lon);
    if (!Number.isFinite(latNum) || !Number.isFinite(lonNum)) {
      continue;
    }
    const d = haversineDistance(centerLat, centerLon, latNum, lonNum);
    if (d > radius) {
      continue;
    }

    const n = String(item.name || "");
    if (!includeMoto && n.includes("機車") && !n.includes("汽")) {
      continue;
    }

    const carTotal = item.car_total;
    if (carTotal !== null && carTotal !== undefined) {
      const totNum = toStrictInt(carTotal);
      if (totNum !== null && totNum !== 0 && totNum < minTotal) {
        continue;
      }
    }

    const freshness = calculateFreshness(item.car_time, nowMs);
    const rateSummary = extractRateSummary(item.pay_info);

    const itemCopy: Record<string, any> = { ...item };
    itemCopy.distance_m = Math.round(d);
    itemCopy.geo_precision = item.geo_precision || "exact";
    itemCopy.fresh_level = freshness.fresh_level;
    itemCopy.fresh_text = freshness.fresh_text;
    itemCopy.freshness = freshness.fresh_text;
    itemCopy.rate_summary = rateSummary;
    itemCopy._internal_freshness = freshness;

    results.push(itemCopy);
  }

  function availRank(item: Record<string, any>): number {
    const v = item.car_value;
    if (item._internal_freshness.is_expired || v === null || v === undefined) {
      return 1;
    }
    const num = toStrictInt(v);
    if (num === null) return 1;
    return num > 0 ? 0 : 2;
  }

  const BAND_M = 500;
  results.sort((a, b) => {
    const bandA = Math.floor(a.distance_m / BAND_M);
    const bandB = Math.floor(b.distance_m / BAND_M);
    if (bandA !== bandB) return bandA - bandB;

    const rankA = availRank(a);
    const rankB = availRank(b);
    if (rankA !== rankB) return rankA - rankB;

    return a.distance_m - b.distance_m;
  });

  return results.map((r) => {
    const expired = r._internal_freshness.is_expired;
    const key = availabilityKey(r.car_value, expired);
    const availStatus = AVAIL_LABELS[key][2];
    const { _internal_freshness, ...cleanRecord } = r;
    cleanRecord.navigation_url = `https://www.google.com/maps/dir/?api=1&destination=${r.lat},${r.lon}`;
    cleanRecord.availability_status = availStatus;
    return cleanRecord;
  });
}

export async function fetchAll(
  lat: number,
  lon: number,
  radiusM: number = 0.0,
  env: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  const tasks: Promise<any>[] = [];

  // 1. Parkboss
  tasks.push(
    fetchParkingSpaces(lat, lon, env, ctx).catch((err) => {
      console.error(`[warn] parkboss 取不到：${err?.message || err}`);
      return [];
    })
  );

  // 2. County Adapters
  const pickedSlugs = adaptersFor(lat, lon, radiusM);
  for (const slug of pickedSlugs) {
    tasks.push(
      fetchAdapter(slug, lat, lon, radiusM, env, ctx).catch((err) => {
        console.error(`[warn] ${slug} 取不到：${err?.message || err}`);
        return [];
      })
    );
  }

  // 3. TDX
  tasks.push(
    fetchTdx(lat, lon, radiusM, env, ctx).catch((err) => {
      console.error(`[warn] tdx 取不到：${err?.message || err}`);
      return [];
    })
  );

  // 4. TDX Overlay
  tasks.push(
    availabilityOverlay(lat, lon, radiusM, env, ctx).catch(() => [])
  );

  const results = await Promise.allSettled(tasks);

  const pbRecords: Record<string, any>[] =
    results[0].status === "fulfilled" && Array.isArray(results[0].value) ? results[0].value : [];

  const adapterRecords: Record<string, any>[] = [];
  for (let i = 1; i <= pickedSlugs.length; i++) {
    const res = results[i];
    if (res.status === "fulfilled" && Array.isArray(res.value)) {
      adapterRecords.push(...res.value);
    }
  }

  const tdxIndex = 1 + pickedSlugs.length;
  const tdxRecords: Record<string, any>[] =
    results[tdxIndex].status === "fulfilled" && Array.isArray(results[tdxIndex].value)
      ? results[tdxIndex].value
      : [];

  const overlayIndex = tdxIndex + 1;
  const overlayRecords: Record<string, any>[] =
    results[overlayIndex].status === "fulfilled" && Array.isArray(results[overlayIndex].value)
      ? results[overlayIndex].value
      : [];

  const allRecords = [...pbRecords, ...adapterRecords, ...tdxRecords];
  const merged = deduplicateRecords(allRecords);

  if (overlayRecords.length > 0) {
    applyAvailabilityOverlay(merged, overlayRecords);
  }

  return merged;
}
