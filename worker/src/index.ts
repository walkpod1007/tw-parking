import { Env } from "./types";
import { handleRpcMessage } from "./mcp";

const MAX_BODY = 256 * 1024;
const MAX_BATCH = 8;

function jsonResponse(data: any, status: number = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...headers
    }
  });
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const startTime = Date.now();
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");

    let response: Response;

    try {
      if (request.method === "GET") {
        if (path.endsWith("/health") || path === "/health") {
          response = jsonResponse({ ok: true, service: "tw-parking-mcp" }, 200);
        } else {
          response = jsonResponse({ error: "not found" }, 404);
        }
      } else if (request.method === "POST") {
        // 1. Auth check
        const isOpen =
          env.TW_PARKING_OPEN === "1" ||
          env.TW_PARKING_OPEN === "true" ||
          env.TW_PARKING_OPEN === "yes";
        const token = (env.TW_PARKING_TOKEN || "").trim();

        if (!isOpen && !token) {
          response = jsonResponse(
            { error: "service unavailable: no token configured and open mode disabled" },
            503
          );
        } else if (!isOpen && request.headers.get("Authorization") !== `Bearer ${token}`) {
          response = jsonResponse({ error: "unauthorized" }, 401);
        } else {
          // 2. Rate limiter check if bound
          if (env.RATE_LIMITER) {
            const clientIp =
              request.headers.get("CF-Connecting-IP") ||
              request.headers.get("X-Forwarded-For")?.split(",")[0]?.trim() ||
              "unknown";
            try {
              const res = await env.RATE_LIMITER.limit({ key: clientIp.slice(0, 64) });
              if (!res.success) {
                return jsonResponse({ error: "rate limited", retry_after: 60 }, 429, {
                  "Retry-After": "60"
                });
              }
            } catch {
              // ignore rate limiter failure
            }
          }

          // 3. Body validation
          const clHeader = request.headers.get("Content-Length");
          if (clHeader !== null) {
            const cl = parseInt(clHeader, 10);
            if (!isNaN(cl) && cl > MAX_BODY) {
              response = jsonResponse(
                {
                  jsonrpc: "2.0",
                  id: null,
                  error: { code: -32600, message: "request entity too large" }
                },
                413
              );
            }
          }

          if (!response!) {
            const buf = await request.arrayBuffer();
            if (buf.byteLength > MAX_BODY) {
              response = jsonResponse(
                {
                  jsonrpc: "2.0",
                  id: null,
                  error: { code: -32600, message: "request entity too large" }
                },
                413
              );
            } else if (buf.byteLength === 0) {
              response = jsonResponse({ error: "bad body size" }, 400);
            } else {
              const rawBody = new TextDecoder().decode(buf);
              let msg: any;
              try {
                msg = JSON.parse(rawBody);
              } catch {
                response = jsonResponse(
                  {
                    jsonrpc: "2.0",
                    id: null,
                    error: { code: -32700, message: "parse error" }
                  },
                  400
                );
              }

              if (!response!) {
                if (!msg || typeof msg !== "object") {
                  response = jsonResponse(
                    {
                      jsonrpc: "2.0",
                      id: null,
                      error: { code: -32600, message: "invalid request" }
                    },
                    400
                  );
                } else if (Array.isArray(msg) && msg.length > MAX_BATCH) {
                  response = jsonResponse(
                    {
                      jsonrpc: "2.0",
                      id: null,
                      error: { code: -32600, message: `批次上限 ${MAX_BATCH} 筆` }
                    },
                    400
                  );
                } else {
                  const msgs = Array.isArray(msg) ? msg : [msg];
                  const out: any[] = [];

                  for (const m of msgs) {
                    if (!m || typeof m !== "object" || Array.isArray(m)) {
                      out.push({
                        jsonrpc: "2.0",
                        id: null,
                        error: { code: -32600, message: "invalid request" }
                      });
                      continue;
                    }

                    try {
                      const resp = await handleRpcMessage(m, env, ctx);
                      if (resp !== null) {
                        out.push(resp);
                      }
                    } catch (err: any) {
                      console.error("handler error:", err);
                      out.push({
                        jsonrpc: "2.0",
                        id: m.id ?? null,
                        error: { code: -32603, message: "internal error" }
                      });
                    }
                  }

                  if (out.length === 0) {
                    response = new Response(null, {
                      status: 202,
                      headers: { "Content-Length": "0" }
                    });
                  } else {
                    response = jsonResponse(Array.isArray(msg) ? out : out[0], 200);
                  }
                }
              }
            }
          }
        }
      } else {
        response = jsonResponse({ error: "method not allowed" }, 405);
      }
    } catch (err: any) {
      console.error("top-level error:", err);
      response = jsonResponse({ error: "internal error" }, 500);
    }

    const ms = Date.now() - startTime;
    const rawPath = url.pathname || "";
    const displayPath = rawPath.length > 8 ? rawPath.slice(0, 7) + "…" : rawPath;
    console.log(`${request.method} ${displayPath} ${response.status} ${ms}`);

    return response;
  }
};
