import { isAnthropicInstanceId } from "../providers/anthropic-instance-id";
import { resolveCodexAccountTargetFromRows } from "./account-target";
import { runCatalogAction } from "./catalog-command-result";
import { serializeManagementJson } from "./json-input";
import { creditReceipt, grantsSchema, parseDto, record, rosterSchema } from "./account-policy-dto";
import { CliUsageError, RuntimeApiError, printData, runtimeBaseUrl, runtimeRequest, summaryLines,
  takeFlag, takeOptionWithSyntax, type RuntimeApiDeps } from "./runtime-api";

type Subcommand = "credits" | "quota-activation" | "anthropic-reset-grants";
const USAGE = "Usage: ocx account credits openai <id|alias|main> <on|off> [--json]\n"
  + "       ocx account quota-activation openai <id|alias|main> --window <fiveHour|weekly> <on|off> [--json]\n"
  + "       ocx account anthropic-reset-grants [ID] [--provider anthropic|anthropic2] [--json]";
const READ_ERRORS = { no_account: "No matching Anthropic OAuth account.", auth_failed: "Sign in to this Anthropic account again.",
  upstream_unavailable: "Anthropic did not return the reset-grant status.", ledger_unavailable: "The reset journal is unavailable.",
  ledger_busy: "The reset journal is busy. No grant was consumed." };
function usage(): never { throw new CliUsageError("Invalid account policy arguments.", USAGE); }
function onOff(value: string | undefined): boolean { if (value !== "on" && value !== "off") usage(); return value === "on"; }
function selector(value: string | undefined): string {
  if (!value || value.trim() !== value || value.length > 4096 || /[\x00-\x1f\x7f]/.test(value) || value.startsWith("-")) usage();
  return value;
}
function option(args: string[], name: string): string | undefined { return takeOptionWithSyntax(args, name)?.value; }
function done(args: string[]): void { if (args.length) usage(); }
async function request(path: string, deps: RuntimeApiDeps, body?: Record<string, unknown>): Promise<unknown> {
  return runtimeRequest(path, { redirect: "error", ...(body === undefined ? {} : {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: serializeManagementJson(body),
  }) }, deps);
}
async function roster(deps: RuntimeApiDeps) {
  return parseDto(rosterSchema, await request("/api/codex-auth/accounts", deps)).accounts;
}
async function target(deps: RuntimeApiDeps, requested: string) {
  const rows = await roster(deps);
  const selected = resolveCodexAccountTargetFromRows(rows, requested);
  if (!("id" in selected)) {
    if ("error" in selected && selected.kind === "not_found") throw new RuntimeApiError("Account not found", 404, null);
    throw new CliUsageError("Account selector is reserved, ambiguous or unavailable. Use a unique account ID.");
  }
  return selected.id;
}
function output(data: Record<string, unknown>, json: boolean, note?: string): number {
  printData(data, json, [...(note ? [note] : []), ...summaryLines(data)]);
  return 0;
}
function requireEqual(actual: unknown, expected: unknown): void { if (actual !== expected) throw new Error("Unverified account policy result"); }

async function codex(sub: "credits" | "quota-activation", args: string[], json: boolean, deps: RuntimeApiDeps): Promise<number> {
  if (args.shift() !== "openai") usage();
  const window = sub === "quota-activation" ? option(args, "--window") : undefined;
  if (sub === "quota-activation" && window !== "fiveHour" && window !== "weekly") usage();
  const requested = selector(args.shift()), enabled = onOff(args.shift());
  done(args);
  const pinned = { ...deps, baseUrl: await runtimeBaseUrl(deps) };
  const id = await target(pinned, requested);
  if (sub === "credits") {
    const result = parseDto(creditReceipt, await request("/api/codex-auth/accounts/credits", pinned, { id, creditsAfterLimit: enabled }));
    requireEqual(result.id, id); requireEqual(result.creditsAfterLimit, enabled);
    return output(result, json, enabled ? "Paid-credit use enabled for this account." : "Paid-credit use disabled for this account.");
  }
  const reply = record(await request("/api/settings", pinned, { codexQuotaAutoRefresh: { id, window, enabled } }));
  requireEqual(reply.ok, true);
  const settings = record(reply.codexQuotaAutoRefresh);
  // Disabling the last window removes the settings entry, by the existing server contract.
  const setting = Object.hasOwn(settings, id) ? record(settings[id]) : { fiveHour: false, weekly: false };
  if (typeof setting.fiveHour !== "boolean" || typeof setting.weekly !== "boolean") throw new Error("Invalid window settings");
  requireEqual(setting[window!], enabled);
  const selected = (await roster(pinned)).find(row => row.id === id);
  if (!selected || selected.quota === undefined) throw new Error("Missing quota evidence after save");
  const available = selected.quotaAutoRefresh
    ? selected.quotaAutoRefresh[window === "fiveHour" ? "fiveHourAvailable" : "weeklyAvailable"]
    : window === "fiveHour" ? selected.quota?.shortWindowSeconds === 18000 && typeof selected.quota.shortResetAt === "number"
      : typeof selected.quota?.weeklyResetAt === "number";
  return output({ ok: true, id, window, enabled: setting[window!], available }, json, "Quota activation setting saved; scheduled refresh is separate.");
}

export async function handleAccountPolicyCommand(sub: Subcommand, args: string[], deps: RuntimeApiDeps = {}): Promise<number> {
  return runCatalogAction(async () => {
    const rest = [...args], json = takeFlag(rest, "--json");
    if (rest.includes("--json")) usage();
    if (sub !== "anthropic-reset-grants") return codex(sub, rest, json, deps);
    const selectedProvider = option(rest, "--provider");
    const provider = selectedProvider ?? "anthropic";
    if (!isAnthropicInstanceId(provider)) usage();
    const id = rest.length ? selector(rest.shift()) : undefined;
    done(rest);
    const pinned = { ...deps, baseUrl: await runtimeBaseUrl(deps) };
    // Omission preserves legacy A request syntax; explicit calls verify the echoed instance.
    const query = new URLSearchParams();
    if (id) query.set("accountId", id);
    if (selectedProvider !== undefined) query.set("provider", provider);
    const result = parseDto(grantsSchema, await request(`/api/anthropic/reset-grants${query.size ? `?${query}` : ""}`, pinned));
    if (selectedProvider !== undefined || result.provider !== undefined) requireEqual(result.provider, provider);
    if (id) requireEqual(result.accountId, id);
    return output(result, json, "Reset-grant status read; no grant was consumed.");
  }, READ_ERRORS);
}
