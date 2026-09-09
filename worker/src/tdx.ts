import { Env } from "./types";
import { fetchWithCache } from "./sources";

const AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token";
const API_BASE = "https://tdx.transportdata.tw/api/basic/v1/Parking/OffStreet";

const STATIC_TTL = 7 * 24 * 3600; // 7 days
const AVAIL_TTL = 60;             // 1 minute

export const TDX_CITY_BOXES: [string, number, number, number, number][] = [
  ["MiaoliCounty",   24.28, 24.78, 120.58, 121.18],
  ["PingtungCounty", 21.85, 22.92, 120.32, 120.98],
  ["KinmenCounty",   24.34, 24.58, 118.16, 118.52]
];

export function citiesFor(lat: number, lon: number, radiusM: number = 0.0): string[] {
  const dlat = radiusM / 111000.0;
  const dlon = radiusM ? radiusM / (111000.0 * 0.914) : 0.0;
  const qla0 = lat - dlat;
  const qla1 = lat + dlat;
  const qlo0 = lon - dlon;
  const qlo1 = lon + dlon;

  const out: string[] = [];
  for (const [city, la0, la1, lo0, lo1] of TDX_CITY_BOXES) {
    if (qla0 <= la1 && qla1 >= la0 && qlo0 <= lo1 && qlo1 >= lo0) {
      out.push(city);
    }
  }
  return out;
}

export async function getTdxToken(env?: Env): Promise<string | null> {
  const cid = env?.TDX_CLIENT_ID?.trim();
  const secret = env?.TDX_CLIENT_SECRET?.trim();
  if (!cid || !secret) {
    return null;
  }

  if (env?.TOKENS) {
    try {
      const cached = await env.TOKENS.get("tdx_token");
      if (cached) {
        return cached;
      }
    } catch {
      // ignore KV read error
    }
  }

  const body = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: cid,
    client_secret: secret
  }).toString();

  try {
    const resp = await fetch(AUTH_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded"
      },
      body,
      signal: AbortSignal.timeout(8000)
    });

    if (!resp.ok) {
      return null;
    }

    const data: any = await resp.json();
    const token = data.access_token;
    if (!token) return null;

    const ttl = (data.expires_in || 86400) - 60;
    if (ttl > 0 && env?.TOKENS) {
      try {
        await env.TOKENS.put("tdx_token", token, { expirationTtl: Math.max(60, ttl) });
      } catch {
        // ignore KV write error
      }
    }
    return token;
  } catch {
    return null;
  }
}

async function apiGet(path: string, token: string, ttl: number, env?: Env, ctx?: ExecutionContext): Promise<any> {
  const url = `${API_BASE}/${path}?%24format=JSON`;
  try {
    const text = await fetchWithCache(
      url,
      {
        headers: {
          authorization: `Bearer ${token}`
        },
        timeoutMs: 8000,
        ttlOverride: ttl
      },
      env,
      ctx
    );
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function getCarparks(city: string, token: string, env?: Env, ctx?: ExecutionContext): Promise<any[]> {
  const data = await apiGet(`CarPark/City/${city}`, token, STATIC_TTL, env, ctx);
  if (!data || typeof data !== "object") return [];
  return data.CarParks || [];
}

async function getAvailability(
  city: string,
  token: string,
  env?: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>> {
  const data = await apiGet(`ParkingAvailability/City/${city}`, token, AVAIL_TTL, env, ctx);
  if (!data || typeof data !== "object") return {};
  const availList = data.ParkingAvailabilities || [];
  const map: Record<string, any> = {};
  for (const a of availList) {
    if (a && a.CarParkID) {
      map[a.CarParkID] = a;
    }
  }
  return map;
}

export async function fetchTdx(
  lat: number,
  lon: number,
  radiusM: number = 0.0,
  env?: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  const cities = citiesFor(lat, lon, radiusM);
  if (cities.length === 0) {
    return [];
  }

  const token = await getTdxToken(env);
  if (!token) {
    return [];
  }

  const out: Record<string, any>[] = [];
  for (const city of cities) {
    const parks = await getCarparks(city, token, env, ctx);
    if (!parks || parks.length === 0) continue;
    const avail = await getAvailability(city, token, env, ctx);

    for (const p of parks) {
      const pos = p.CarParkPosition || {};
      const plat = pos.PositionLat;
      const plon = pos.PositionLon;
      if (plat === null || plat === undefined || plon === null || plon === undefined) continue;
      const latF = parseFloat(plat);
      const lonF = parseFloat(plon);
      if (isNaN(latF) || isNaN(lonF)) continue;

      const name = (p.CarParkName && p.CarParkName.Zh_tw) || p.CarParkID;
      const a = avail[p.CarParkID] || {};

      let total = p.TotalSpaces;
      if (total === null || total === undefined || total === 0) {
        total = a.TotalSpaces;
      }

      out.push({
        name,
        lat: latF,
        lon: lonF,
        car_total: total !== null && total !== undefined ? parseInt(String(total), 10) : null,
        car_value: a.AvailableSpaces !== null && a.AvailableSpaces !== undefined ? parseInt(String(a.AvailableSpaces), 10) : null,
        car_time: a.DataCollectTime || null,
        pay_info: p.FareDescription || null,
        source: `tdx:${city}`,
        geo_precision: "exact"
      });
    }
  }
  return out;
}

export async function availabilityOverlay(
  lat: number,
  lon: number,
  radiusM: number = 0.0,
  env?: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  const cities = citiesFor(lat, lon, radiusM);
  if (cities.length === 0) {
    return [];
  }

  const token = await getTdxToken(env);
  if (!token) {
    return [];
  }

  const out: Record<string, any>[] = [];
  for (const city of cities) {
    const parks = await getCarparks(city, token, env, ctx);
    const known = new Set(parks.map((p) => p.CarParkID));
    const avail = await getAvailability(city, token, env, ctx);
    const boxEntry = TDX_CITY_BOXES.find((b) => b[0] === city);
    const box = boxEntry ? [boxEntry[1], boxEntry[2], boxEntry[3], boxEntry[4]] : null;

    for (const [cid, a] of Object.entries(avail)) {
      if (known.has(cid)) continue;
      const name = (a.CarParkName && a.CarParkName.Zh_tw);
      if (!name) continue;

      out.push({
        name,
        car_value: a.AvailableSpaces !== null && a.AvailableSpaces !== undefined ? parseInt(String(a.AvailableSpaces), 10) : null,
        car_total: a.TotalSpaces !== null && a.TotalSpaces !== undefined ? parseInt(String(a.TotalSpaces), 10) : null,
        car_time: a.DataCollectTime || null,
        box
      });
    }
  }
  return out;
}
