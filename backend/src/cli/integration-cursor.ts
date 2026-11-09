/** Read-only Cursor integration observations; never downloads or configures Cursor. */
import type { CursorIntegrationStatus } from "../server/management/cursor-integration-routes";
import { runCatalogAction } from "./catalog-command-result";
import { CliUsageError, printData, runtimeRequest, takeFlag, type RuntimeApiDeps } from "./runtime-api";

const USAGE = "Usage: ocx integration native cursor [status] [--json]";
const GUIDE_URL = "https://lidge-jun.github.io/opencodex/guides/cursor-private-inference/";
const PLACEHOLDER = "opencodex-loopback";

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid Cursor response");
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  if (typeof value !== "string" || !value.length) throw new Error("Invalid Cursor text");
  return value;
}
function nullableText(value: unknown): string | null { return value === null ? null : text(value); }
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Invalid Cursor flag");
  return value;
}
function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error("Invalid Cursor number");
  return value;
}
function strings(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("Invalid Cursor list");
  return value.map(text);
}

function statusDto(raw: unknown): CursorIntegrationStatus {
  const value = record(raw), privateInference = record(value.privateInference), regular = record(value.regularCursor);
  const gateway = record(value.gateway), table = record(value.effortTable);
  if (!Array.isArray(value.models) || (gateway.apiKeyMode !== "credential" && gateway.apiKeyMode !== "placeholder")
    || gateway.placeholder !== PLACEHOLDER || (table.source !== "bundle" && table.source !== "static")
    || value.guideUrl !== GUIDE_URL) throw new Error("Invalid Cursor status");
  const baseUrl = text(gateway.baseUrl), parsedUrl = new URL(baseUrl);
  if (!["http:", "https:"].includes(parsedUrl.protocol) || parsedUrl.username || parsedUrl.password
    || parsedUrl.search || parsedUrl.hash || parsedUrl.pathname !== "/v1") throw new Error("Invalid Cursor gateway");
  const last = value.lastSeen === null ? null : record(value.lastSeen);
  return {
    privateInference: { installed: boolean(privateInference.installed), path: nullableText(privateInference.path), version: nullableText(privateInference.version) },
    regularCursor: { installed: boolean(regular.installed), path: nullableText(regular.path) },
    gateway: { baseUrl, apiKeyMode: gateway.apiKeyMode, placeholder: PLACEHOLDER },
    lastSeen: last === null ? null : { at: integer(last.at), userAgent: text(last.userAgent) },
    effortTable: { source: table.source, version: nullableText(table.version), families: table.families === null ? null : integer(table.families) },
    models: value.models.map(rawModel => {
      const model = record(rawModel), context = model.context === null ? null : record(model.context);
      return { id: text(model.id), reasoning: model.reasoning === null ? null : strings(model.reasoning), family: nullableText(model.family),
        tableLess: boolean(model.tableLess), effortRows: strings(model.effortRows),
        context: context === null ? null : { defaultWindow: integer(context.defaultWindow), longWindow: integer(context.longWindow) } };
    }),
    guideUrl: GUIDE_URL,
  };
}

/** Arguments start immediately after `integration native cursor`. */
export async function handleCursorIntegrationCommand(argv: string[], deps: RuntimeApiDeps = {}): Promise<number> {
  return runCatalogAction(async () => {
    const args = [...argv], wantsJson = takeFlag(args, "--json"), action = args.shift() ?? "status";
    if (args.length || action !== "status") {
      throw new CliUsageError("Cursor supports only status reads.", USAGE);
    }
    const raw = await runtimeRequest("/api/native-integrations/cursor", { method: "GET", redirect: "error" }, deps);
    const result = statusDto(raw);
    printData(result, wantsJson, [
      `Cursor Private Inference: ${result.privateInference.installed ? "installed" : "not found"}`,
      `Regular Cursor: ${result.regularCursor.installed ? "installed" : "not found"}`,
      `Gateway: ${result.gateway.baseUrl}`,
      result.gateway.apiKeyMode === "credential"
        ? "API key: an existing proxy credential is required. The placeholder is not an access credential."
        : `API key placeholder: ${PLACEHOLDER} (not a secret).`,
      `Last seen: ${result.lastSeen ? `${result.lastSeen.userAgent} at ${result.lastSeen.at}` : "no Cursor request observed since proxy startup"}`,
      `Models: ${result.models.length}`,
      ...result.models.map(model => `${model.id}: reasoning ${model.reasoning?.join(", ") ?? "not advertised"}`),
      `Guide: ${result.guideUrl}`,
    ]);
    return 0;
  });
}
