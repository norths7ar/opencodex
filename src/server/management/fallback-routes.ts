import type { ManagementContext } from "./context";
import { jsonResponse } from "../auth-cors";
import { mutatePersistedConfig } from "../../config";
import { applyFallbackWrite, buildFallbackState, parseFallbackWrites } from "../../routing/fallback-state";
import { managementBodyTooLargeResponse, readManagementJsonBody } from "./body";

function view(ctx: ManagementContext): Response {
  const response = jsonResponse(buildFallbackState(ctx.config), 200, ctx.req, ctx.config);
  response.headers.set("cache-control", "no-store");
  return response;
}

export async function handleFallbackRoutes(ctx: ManagementContext): Promise<Response | null> {
  const { req, url, config } = ctx;
  if (url.pathname === "/api/fallback" && req.method === "GET") return view(ctx);
  if (url.pathname === "/api/fallback" && req.method === "PUT") return await writeFallback(ctx);
  return null;
}

async function writeFallback(ctx: ManagementContext): Promise<Response> {
  const { req, config } = ctx;
  let body: unknown;
  try {
    body = await readManagementJsonBody(req);
  } catch (error) {
    return managementBodyTooLargeResponse(error, req, config)
      ?? jsonResponse({ error: "invalid JSON body", code: "invalid_request" }, 400, req, config);
  }
  const parsed = parseFallbackWrites(body);
  if (!parsed.ok) return jsonResponse({ error: parsed.error, code: "invalid_request" }, 400, req, config);
  for (const write of parsed.writes) {
    if (write.scope === "device" && write.value !== null && !(config.apiKeys ?? []).some(entry => entry.id === write.key)) {
      return jsonResponse({ error: "unknown device: use a configured API key id", code: "unknown_device" }, 404, req, config);
    }
  }
  const outcome = mutatePersistedConfig(persisted => {
    let changed = false;
    for (const write of parsed.writes) changed = applyFallbackWrite(persisted, write) || changed;
    return { changed, value: persisted.fallback === undefined ? undefined : structuredClone(persisted.fallback) };
  });
  if (outcome.status === "unavailable") {
    return jsonResponse({ error: "config.json is " + outcome.reason + "; fallback settings were not saved", code: "config_unavailable" }, 409, req, config);
  }
  if (outcome.value === undefined) delete config.fallback;
  else config.fallback = outcome.value;
  return view(ctx);
}
