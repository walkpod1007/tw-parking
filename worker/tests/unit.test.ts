import { describe, it, expect, vi } from "vitest";
import {
  haversineDistance,
  extractRateSummary,
  calculateFreshness,
  availabilityKey,
  AVAIL_LABELS,
  isSameParkingLot,
  shouldReplace,
  deduplicateRecords,
  parseCarTime
} from "../src/parking";
import { fetchWithCache, pickTaitungCandidates } from "../src/sources";
import worker from "../src/index";
import { Env } from "../src/types";
describe("1. Haversine Distance", () => {
  it("returns 0 for identical coordinates", () => {
    const d = haversineDistance(25.033976, 121.564472, 25.033976, 121.564472);
    expect(d).toBe(0);
  });

  it("calculates accurate distance between Taipei 101 and Taipei City Hall", () => {
    // Approx 400m
    const d = haversineDistance(25.033976, 121.564472, 25.0375, 121.5637);
    expect(d).toBeGreaterThan(380);
    expect(d).toBeLessThan(420);
  });

  it("calculates known distance between Taipei and Kaohsiung (~290km - 310km)", () => {
    const d = haversineDistance(25.0478, 121.5170, 22.6273, 120.3014);
    expect(d).toBeGreaterThan(290000);
    expect(d).toBeLessThan(320000);
  });
});

describe("2. Deduplication", () => {
  it("never deduplicates within the same source (e.g. roadside sensors)", () => {
    const rec1 = {
      name: "復興路",
      lat: 23.900,
      lon: 120.680,
      source: "parkboss",
      car_value: 1
    };
    const rec2 = {
      name: "復興路",
      lat: 23.9001,
      lon: 120.6801,
      source: "parkboss",
      car_value: 0
    };
    expect(isSameParkingLot(rec1, rec2)).toBe(false);
    const deduped = deduplicateRecords([rec1, rec2]);
    expect(deduped).toHaveLength(2);
  });

  it("deduplicates cross-source lots within 50m where names match", () => {
    const rec1 = {
      name: "台北車站西側地上停車場",
      lat: 25.0476,
      lon: 121.5165,
      source: "static_store",
      car_value: null
    };
    const rec2 = {
      name: "臺北車站西側地上停車場",
      lat: 25.04762,
      lon: 121.51653,
      source: "parkboss",
      car_value: 23
    };
    // Names contain each other and distance < 50m
    // Notice "台北" and "臺北": check shouldReplace and replacement
    const rA = { ...rec1, name: "車站西側地上停車場" };
    const rB = { ...rec2, name: "車站西側地上停車場" };
    expect(isSameParkingLot(rA, rB)).toBe(true);
    expect(shouldReplace(rB, rA)).toBe(true);

    const deduped = deduplicateRecords([rA, rB]);
    expect(deduped).toHaveLength(1);
    expect(deduped[0].source).toBe("parkboss");
    expect(deduped[0].car_value).toBe(23);
  });

  it("does not deduplicate lots further than 50m even with identical names", () => {
    const rec1 = {
      name: "第一停車場",
      lat: 25.000,
      lon: 121.500,
      source: "sourceA"
    };
    const rec2 = {
      name: "第一停車場",
      lat: 25.005, // ~550m away
      lon: 121.500,
      source: "sourceB"
    };
    expect(isSameParkingLot(rec1, rec2)).toBe(false);
    expect(deduplicateRecords([rec1, rec2])).toHaveLength(2);
  });
});

describe("3. Freshness Ranking & 24h Boundary", () => {
  const baseNow = Date.UTC(2026, 8, 9, 6, 0, 0); // 2026-09-09 14:00:00 Taipei time

  it("classifies <= 30 minutes as live", () => {
    // 10 minutes ago
    const t = "2026-09-09 13:50:00";
    const res = calculateFreshness(t, baseNow);
    expect(res.fresh_level).toBe("live");
    expect(res.fresh_text).toBe("即時");
    expect(res.is_expired).toBe(false);
  });

  it("classifies between 30 min and 24 hours as recent", () => {
    // 3 hours ago
    const t = "2026-09-09 11:00:00";
    const res = calculateFreshness(t, baseNow);
    expect(res.fresh_level).toBe("recent");
    expect(res.fresh_text).toBe("3 小時前");
    expect(res.is_expired).toBe(false);
  });

  it("tests 24-hour boundary: 86400s is recent, 86401s is expired", () => {
    // Exactly 24 hours ago (86400s)
    const exact24hMs = baseNow - 86400 * 1000;
    const exactIso = new Date(exact24hMs + 8 * 3600 * 1000).toISOString();
    const exactStr = exactIso.slice(0, 10) + " " + exactIso.slice(11, 19);
    const res24 = calculateFreshness(exactStr, baseNow);
    expect(res24.fresh_level).toBe("recent");
    expect(res24.fresh_text).toBe("24 小時前");
    expect(res24.is_expired).toBe(false);

    // 24 hours + 10 seconds ago
    const over24hMs = baseNow - 86410 * 1000;
    const overIso = new Date(over24hMs + 8 * 3600 * 1000).toISOString();
    const overStr = overIso.slice(0, 10) + " " + overIso.slice(11, 19);
    const resOver = calculateFreshness(overStr, baseNow);
    expect(resOver.fresh_level).toBe("expired");
    expect(resOver.is_expired).toBe(true);
    expect(resOver.fresh_text).toContain("數字過期（最後更新 2026-09-08）");
  });

  it("handles missing car_time as never (無即時回報)", () => {
    const resNull = calculateFreshness(null, baseNow);
    expect(resNull.fresh_level).toBe("never");
    expect(resNull.fresh_text).toBe("無即時回報");
    expect(resNull.is_expired).toBe(true);

    const resEmpty = calculateFreshness("", baseNow);
    expect(resEmpty.fresh_level).toBe("never");
    expect(resEmpty.fresh_text).toBe("無即時回報");
    expect(resEmpty.is_expired).toBe(true);
  });

  it("handles unparseable car_time as expired with prefix", () => {
    const res = calculateFreshness("2024-invalid-time", baseNow);
    expect(res.fresh_level).toBe("expired");
    expect(res.fresh_text).toBe("數字過期（最後更新 2024-inval）");
    expect(res.is_expired).toBe(true);
  });
});

describe("4. Vacancy Four States", () => {
  it("evaluates > 2 spaces as green/available", () => {
    const key = availabilityKey(5, false);
    expect(key).toBe("green");
    const [emoji, word, status] = AVAIL_LABELS[key];
    expect(emoji).toBe("🟢");
    expect(word).toBe("有位");
    expect(status).toBe("available");
  });

  it("evaluates 1 or 2 spaces as low", () => {
    expect(availabilityKey(2, false)).toBe("low");
    expect(availabilityKey(1, false)).toBe("low");
    const [emoji, word, status] = AVAIL_LABELS["low"];
    expect(emoji).toBe("🟡");
    expect(word).toBe("快滿");
    expect(status).toBe("low");
  });

  it("evaluates 0 spaces as full", () => {
    const key = availabilityKey(0, false);
    expect(key).toBe("full");
    const [emoji, word, status] = AVAIL_LABELS[key];
    expect(emoji).toBe("🔴");
    expect(word).toBe("已滿");
    expect(status).toBe("full");
  });

  it("evaluates null, undefined, or expired values as unknown", () => {
    expect(availabilityKey(null, false)).toBe("unknown");
    expect(availabilityKey(10, true)).toBe("unknown"); // expired drops to unknown
    expect(availabilityKey("bad", false)).toBe("unknown");
    const [emoji, word, status] = AVAIL_LABELS["unknown"];
    expect(emoji).toBe("❓");
    expect(word).toBe("空位不明");
    expect(status).toBe("unknown");
  });
});

describe("5. Fee Summary Extraction", () => {
  it("extracts hourly rate patterns", () => {
    expect(extractRateSummary("計時：100元/時")).toBe("每小時 100 元");
    expect(extractRateSummary("小型車每小時收費30元，機車免費")).toBe("每小時 30 元");
    expect(extractRateSummary("40元/小時")).toBe("每小時 40 元");
  });

  it("extracts half-hourly rate patterns", () => {
    expect(extractRateSummary("每半小時收費25元，當日上限150元")).toBe("每半小時 25 元");
    expect(extractRateSummary("30元/半小時")).toBe("每半小時 30 元");
  });

  it("truncates unknown long text to 16 chars with ellipsis", () => {
    const longText = "月租全日五千元，夜間兩千元，里民優惠七折";
    const summary = extractRateSummary(longText);
    expect(summary).toBe("月租全日五千元，夜間兩千元，里民…");
    expect(summary.endsWith("…")).toBe(true);
  });

  it("returns empty string for null, undefined, or non-string input", () => {
    expect(extractRateSummary(null)).toBe("");
    expect(extractRateSummary(undefined)).toBe("");
    expect(extractRateSummary("")).toBe("");
  });
});

describe("6. Car Time Parsing (H2)", () => {
  it("parses fractional seconds, date-only, and slash-separated formats as Taipei time", () => {
    // 2026-09-08 12:01:20.500 Taipei = 2026-09-08 04:01:20.500 UTC
    const tFraction = parseCarTime("2026-09-08T12:01:20.500");
    expect(tFraction).toBe(Date.UTC(2026, 8, 8, 4, 1, 20, 500));

    // 2026-09-08 00:00:00 Taipei = 2026-09-07 16:00:00 UTC
    const tDateOnly = parseCarTime("2026-09-08");
    expect(tDateOnly).toBe(Date.UTC(2026, 8, 7, 16, 0, 0));

    // 2026/09/08 12:01:20 must return null (slash format not supported in Python parse_car_time)
    const tSlash = parseCarTime("2026/09/08 12:01:20");
    expect(tSlash).toBeNull();
  });

  it("validates calendar dates strictly and handles leap years", () => {
    expect(parseCarTime("2026-02-29")).toBeNull();
    expect(parseCarTime("2026-04-31 10:00:00")).toBeNull();
    expect(parseCarTime("2026-13-01")).toBeNull();
    expect(parseCarTime("2026-06-31T00:00:00")).toBeNull();

    // 2024-02-29 08:00:00 Taipei = 2024-02-29 00:00:00 UTC
    expect(parseCarTime("2024-02-29 08:00:00")).toBe(Date.UTC(2024, 1, 29, 0, 0, 0));
  });

  it("handles explicit timezone Z and +08:00 correctly", () => {
    const tZ = parseCarTime("2026-09-08T04:01:20Z");
    expect(tZ).toBe(Date.UTC(2026, 8, 8, 4, 1, 20, 0));

    const tTz = parseCarTime("2026-09-08T12:01:20+08:00");
    expect(tTz).toBe(Date.UTC(2026, 8, 8, 4, 1, 20, 0));
  });

  it("returns null for invalid or unparseable time string", () => {
    expect(parseCarTime("invalid-timestamp")).toBeNull();
    expect(parseCarTime(null)).toBeNull();
    expect(parseCarTime("")).toBeNull();
  });
});

describe("7. Availability Strict Int (M2)", () => {
  it("rejects non-integer strings and whitespace, returns unknown", () => {
    expect(availabilityKey("3.9", false)).toBe("unknown");
    expect(availabilityKey("5.0", false)).toBe("unknown");
    expect(availabilityKey("  ", false)).toBe("unknown");
  });

  it("accepts valid strict integers as green", () => {
    expect(availabilityKey("5", false)).toBe("green");
    expect(availabilityKey(5, false)).toBe("green");
  });
});

describe("8. Single-Flight In-Flight Deduplication (M1)", () => {
  it("deduplicates concurrent upstream fetches for the same cache key to 1 request", async () => {
    let callCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        callCount++;
        await new Promise((r) => setTimeout(r, 20));
        return new Response("flight-result", { status: 200 });
      })
    );

    try {
      const p1 = fetchWithCache("https://test.api/single-flight", { ttlOverride: 0 });
      const p2 = fetchWithCache("https://test.api/single-flight", { ttlOverride: 0 });
      const [r1, r2] = await Promise.all([p1, p2]);

      expect(callCount).toBe(1);
      expect(r1).toBe("flight-result");
      expect(r2).toBe("flight-result");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("9. Taitung Candidate Picking (H1)", () => {
  const centerLat = 22.754;
  const centerLon = 121.146;
  const radiusM = 1000;

  it("keeps all lots when candidate count within radius is <= 12 (e.g. 5 lots)", () => {
    const lots = [
      { id: 1, PositionLat: 22.7541, PositionLon: 121.1461 },
      { id: 2, PositionLat: 22.7542, PositionLon: 121.1462 },
      { id: 3, PositionLat: 22.7543, PositionLon: 121.1463 },
      { id: 4, PositionLat: 22.7544, PositionLon: 121.1464 },
      { id: 5, PositionLat: 22.7545, PositionLon: 121.1465 },
      { id: 99, PositionLat: 22.8000, PositionLon: 121.2000 }
    ];

    const picked = pickTaitungCandidates(lots, centerLat, centerLon, radiusM);
    expect(picked).toHaveLength(5);
    const ids = picked.map((l) => l.id);
    expect(ids).toEqual([1, 2, 3, 4, 5]);
    expect(ids).not.toContain(99);
  });

  it("truncates to closest 12 lots when candidate count within radius exceeds 12 (e.g. 20 lots)", () => {
    const lots: any[] = [];
    for (let i = 1; i <= 20; i++) {
      lots.push({
        id: i,
        PositionLat: centerLat + i * 0.0003,
        PositionLon: centerLon
      });
    }
    lots.push({ id: 100, PositionLat: centerLat + 0.02, PositionLon: centerLon });

    const picked = pickTaitungCandidates(lots, centerLat, centerLon, radiusM);
    expect(picked).toHaveLength(12);
    const ids = picked.map((l) => l.id);
    expect(ids).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    expect(ids).not.toContain(100);
  });

  it("excludes all lots outside radius", () => {
    const lots = [
      { id: 101, PositionLat: 22.90, PositionLon: 121.30 },
      { id: 102, PositionLat: 22.95, PositionLon: 121.35 }
    ];
    const picked = pickTaitungCandidates(lots, centerLat, centerLon, radiusM);
    expect(picked).toHaveLength(0);
  });
});

describe("10. Request Body Size Limit (M3)", () => {
  const dummyEnv: Env = {
    TOKENS: {} as any,
    TW_PARKING_OPEN: "true"
  };
  const dummyCtx: any = {
    waitUntil: () => {},
    passThroughOnException: () => {}
  };

  it("returns 413 with code -32600 when Content-Length header exceeds 262144 bytes", async () => {
    const req = new Request("http://localhost/", {
      method: "POST",
      headers: {
        "Content-Length": "300000",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" })
    });

    const res = await worker.fetch(req, dummyEnv, dummyCtx);
    expect(res.status).toBe(413);
    const body = await res.json() as any;
    expect(body).toMatchObject({
      jsonrpc: "2.0",
      id: null,
      error: { code: -32600 }
    });
  });

  it("returns 413 with code -32600 for a 300 KB pure Chinese payload", async () => {
    const chineseText = "中".repeat(100000);
    const payload = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "find_parking", arguments: { text: chineseText } }
    });

    const req = new Request("http://localhost/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: payload
    });

    const res = await worker.fetch(req, dummyEnv, dummyCtx);
    expect(res.status).toBe(413);
    const body = await res.json() as any;
    expect(body).toMatchObject({
      jsonrpc: "2.0",
      id: null,
      error: { code: -32600 }
    });
  });
});
