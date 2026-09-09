import { Env } from "./types";
import { fetchAll, processRecords } from "./parking";

export class ValueError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValueError";
  }
}

export const PROTOCOL_FALLBACK = "2025-06-18";
export const MAX_RADIUS_M = 20000.0;
export const MAX_LIMIT = 100;

export const TOOLS = [
  {
    name: "find_parking",
    description: "查詢座標附近的停車場與即時剩餘車位。回傳每個場的名稱、距離、剩餘格數、總格數、資料時間、費率與導航連結。資料超過 24 小時的場不給車位數字。",
    inputSchema: {
      type: "object",
      properties: {
        lat: { type: "number", description: "緯度，例如 25.047697" },
        lon: { type: "number", description: "經度，例如 121.515114" },
        radius: { type: "number", description: "搜尋半徑（公尺），預設 800", default: 800 },
        limit: { type: "integer", description: "最多回傳幾個場，0 代表全部。預設 5", default: 5 },
        include_moto: { type: "boolean", description: "是否連機車專用場一起回傳，預設否", default: false }
      },
      required: ["lat", "lon"]
    }
  }
];

export function numArg(
  args: Record<string, any>,
  key: string,
  defaultVal?: number,
  lo?: number,
  hi?: number,
  integer: boolean = false
): number {
  let raw = args[key];
  if (raw === undefined || raw === null || raw === "") {
    raw = defaultVal;
  }
  if (typeof raw === "boolean" || typeof raw !== "number") {
    throw new ValueError(`${key} 必須是數字（不接受字串或布林值）`);
  }
  const v = raw;
  if (!Number.isFinite(v) || isNaN(v)) {
    throw new ValueError(`${key} 必須是有限數字`);
  }
  if (integer && !Number.isInteger(v)) {
    throw new ValueError(`${key} 必須是整數`);
  }
  if (lo !== undefined && v < lo) {
    throw new ValueError(`${key} 超出範圍（${lo}〜${hi}）`);
  }
  if (hi !== undefined && v > hi) {
    throw new ValueError(`${key} 超出範圍（${lo}〜${hi}）`);
  }
  return v;
}

export async function findParking(
  args: any,
  env: Env,
  ctx?: ExecutionContext
): Promise<Record<string, any>[]> {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new ValueError("arguments 必須是物件");
  }

  const lat = numArg(args, "lat", undefined, -90.0, 90.0);
  const lon = numArg(args, "lon", undefined, -180.0, 180.0);
  const radius = numArg(args, "radius", 800, 1.0, MAX_RADIUS_M);
  const limit = numArg(args, "limit", 5, 0, MAX_LIMIT, true);

  let moto = args.include_moto;
  if (moto === undefined || moto === null) {
    moto = false;
  }
  if (typeof moto !== "boolean") {
    throw new ValueError("include_moto 必須是布林值");
  }

  const rawRecords = await fetchAll(lat, lon, radius, env, ctx);
  let records = processRecords(rawRecords, lat, lon, radius, moto, 6);

  if (limit > 0) {
    records = records.slice(0, limit);
  }

  return records;
}

export async function handleRpcMessage(
  msg: any,
  env: Env,
  ctx?: ExecutionContext
): Promise<any | null> {
  const method = msg.method;
  const mid = msg.id ?? null;

  if (method === "initialize") {
    const clientProto = msg.params?.protocolVersion;
    return {
      jsonrpc: "2.0",
      id: mid,
      result: {
        protocolVersion: clientProto || PROTOCOL_FALLBACK,
        capabilities: { tools: {} },
        serverInfo: { name: "tw-parking", version: "1.0.0" }
      }
    };
  }

  if (method === "notifications/initialized" || method === "initialized") {
    return null;
  }

  if (method === "tools/list") {
    return {
      jsonrpc: "2.0",
      id: mid,
      result: { tools: TOOLS }
    };
  }

  if (method === "tools/call") {
    const params = msg.params || {};
    const name = params.name;
    if (name !== "find_parking") {
      return {
        jsonrpc: "2.0",
        id: mid,
        error: { code: -32602, message: `未知的工具: ${name}` }
      };
    }

    try {
      const records = await findParking(params.arguments || {}, env, ctx);
      return {
        jsonrpc: "2.0",
        id: mid,
        result: {
          content: [
            {
              type: "text",
              text: JSON.stringify(records, null, 1)
            }
          ],
          isError: false
        }
      };
    } catch (e: any) {
      if (e instanceof ValueError) {
        return {
          jsonrpc: "2.0",
          id: mid,
          result: {
            content: [{ type: "text", text: `查詢失敗: ${e.message}` }],
            isError: true
          }
        };
      }
      console.error("tool error:", e);
      return {
        jsonrpc: "2.0",
        id: mid,
        result: {
          content: [{ type: "text", text: "查詢失敗: 上游暫時取不到資料" }],
          isError: true
        }
      };
    }
  }

  if (mid === null) {
    return null;
  }

  return {
    jsonrpc: "2.0",
    id: mid,
    error: { code: -32601, message: `未支援的方法: ${method}` }
  };
}
