/** `ocx account` — inspect and manage each provider channel's credential. */
import { apiKeyQuotaText } from "./account-key-quota";
import { redactSecretArgs } from "./secret-args";
import { emptyAccountNextAction, recoveryAccountLabel } from "./account-next-actions";
import { loadConfig } from "../config";
import type { OcxConfig } from "../types";
import {
  cmdAddKey,
  cmdAlias,
  cmdClearCooldown,
  cmdPause,
  cmdRefresh,
  cmdRemove,
} from "./account-extended";
import { apiError, apiJson, classifyAccount, fetchRows, proxyUnreachable, resolveBaseUrl, type AccountDeps, type AccountRow, type AccountType }
  from "./account-api";

export { classifyAccount } from "./account-api";
export type { AccountDeps, AccountRow, AccountType, ClassifyResult } from "./account-api";
type TargetProvenance = "live-oauth-list" | "config" | "codex";

const MAIN_ALIAS = "main";
const MAIN_CODEX_ID = "__main__";
const ACCOUNT_USAGE = `Usage:
  ocx account list [provider] [--json] [--all] [--quota [--refresh]]
  ocx account history openai <account-id> [--limit <1-200>] [--json]
  ocx account current <provider> [--json]
  ocx account refresh <provider> [--json]
  ocx account alias <provider> <account-or-key-id|alias> <display-name|-> [--json]
  ocx account pause <provider> <account-id|alias|main> [--json]
  ocx account resume <provider> <account-id|alias|main> [--json]
  ocx account remove <provider> <account-or-key-id|alias|main> --yes [--json]
  ocx account clear-cooldown <provider> <account-id|alias|main> [--json]
  ocx account add-key <provider> [--label <label>] [--json]
  ocx account login <provider> [--id <account-id>] [--reauth] [--open-browser on|off] [--add-account on|off] [--code -] [--no-wait] [--json]
  ocx account credits openai <id|alias|main> <on|off> [--json]
  ocx account quota-activation openai ID --window fiveHour|weekly <on|off> [--json]
  ocx account anthropic-reset-grants [ID] [--provider anthropic|anthropic2] [--json]
  ocx account code <provider> [--flow <flow-id>] [--json]   (reads the code from stdin)
  ocx account cancel <provider> [--flow <flow-id>] [--json] (--flow required for codex)
  ocx account reset-credits <account-id|main> [--consume --yes] [--json]
  ocx account grok-reset-coupons [<account-id>] [--consume --yes] [--token-id <token-id>] [--json]
  ocx account reauth <provider> [--id <account-id>] [--device] [--code -] [--no-wait] [--json]
  ocx account main reauth --device [--no-wait] [--json]
  ocx account main reauth <status|cancel> --flow <id> [--json]

Each provider channel uses one account or API key. Configure separate channels to keep separate credentials.
Use ocx logout <provider> to remove a stored OAuth login.`;

function consumeFlag(args: string[], flag: string): boolean {
  const idx = args.indexOf(flag);
  if (idx === -1) return false;
  args.splice(idx, 1);
  return true;
}

/** Returns an error message for leftover args, or null when clean. */
function leftoverArgsError(args: string[]): string | null {
  if (args.length === 0) return null;
  const shown = redactSecretArgs(args);
  // Flags plus redaction markers only: a stray positional may be a credential operand.
  const unknown = shown.filter(a => a.startsWith("-") || a === "<redacted>");
  return unknown.length > 0
    ? `Unknown flag(s): ${unknown.join(", ")}`
    : `Unexpected argument(s): ${shown.join(", ")}`;
}

function candidateNames(config: OcxConfig): string {
  const names = new Set<string>(["openai"]);
  for (const n of Object.keys(config.providers ?? {})) names.add(n);
  return [...names].join(", ");
}

function displayId(id: string): string {
  return id === MAIN_CODEX_ID ? MAIN_ALIAS : id;
}

function statusText(row: AccountRow): string {
  const parts: string[] = [];
  if (row.paused) parts.push(row.pauseDetails ? `paused(${row.pauseDetails.reason})` : "paused");
  if (row.active) parts.push("active");
  if (row.needsReauth && !(row.provider === "kiro" && row.skipReason === "needs_reauth")) {
    parts.push(row.needsReauthReason === "verify_account" ? "needs-reauth(verify)" : "needs-reauth");
  }
  if (row.validationPending) parts.push("validation-pending");
  if (row.health && row.health !== "Healthy" && !row.needsReauth && !row.validationPending) parts.push(row.health.toLowerCase());
  if (row.creditsAfterLimit === true) parts.push("paid-credits: on");
  return parts.join(" ");
}

/**
 * Compact per-account quota for the opt-in QUOTA column: the two windows an operator actually
 * decides on before a long session. The full breakdown stays in `--json`.
 */
function quotaText(row: AccountRow): string {
  if (row.type === "api-key") return apiKeyQuotaText(row);
  if (row.quotaUnavailable) return row.quotaFailure ? `unavailable (${row.quotaFailure})` : "unavailable";
  const quota = row.quota;
  if (!quota) return "-";
  const parts: string[] = [];
  // Two spellings reach this DTO: the per-account provider probe reports `fiveHourPercent`,
  // while the Codex pool reports the same idea as `shortPercent`.
  const short = quota.fiveHourPercent ?? quota.shortPercent;
  if (typeof short === "number") parts.push(`5h ${short}%`);
  if (typeof quota.weeklyPercent === "number") parts.push(`wk ${quota.weeklyPercent}%`);
  // Kiro bills a monthly allowance and reports no shorter window, so without this arm a
  // perfectly healthy Kiro account prints "-" and reads as broken.
  if (typeof quota.monthlyPercent === "number") parts.push(`mo ${Math.round(quota.monthlyPercent)}%`);
  return parts.length > 0 ? parts.join(" ") : "-";
}

export function formatAccountTable(rows: AccountRow[], withQuota = false): string {
  const header = ["PROVIDER", "TYPE", "ID", "PLAN/LABEL", "STATUS"];
  if (withQuota) header.push("QUOTA");
  const data = rows.map(r => {
    const keyLabel = r.masked && r.label !== r.masked ? `${r.masked} (${r.label})` : r.masked;
    const cols = [
      r.provider,
      r.type,
      displayId(r.id),
      r.type === "api-key" ? keyLabel ?? "-" : r.label ?? "-",
      statusText(r),
    ];
    if (withQuota) cols.push(quotaText(r));
    return cols;
  });
  const widths = header.map((h, i) => Math.max(h.length, ...data.map(d => d[i]!.length)));
  const line = (cols: string[]) => cols.map((c, i) => c.padEnd(widths[i]!)).join("  ").trimEnd();
  const actions = rows.flatMap(row => row.healthAction ? [`${row.provider} ${recoveryAccountLabel(row.id, displayId(row.id))}: ${row.health?.toLowerCase() ?? "needs attention"}. Next: ${row.healthAction}`] : []);
  return [line(header), ...data.map(line), ...actions].join("\n");
}

async function cmdList(rest: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = consumeFlag(rest, "--json");
  const showAll = consumeFlag(rest, "--all");
  // Opt-in: the server probes the upstream once per stored credential, so the default listing
  // stays a cheap local read (#2566). --refresh bypasses the server-side TTL.
  const wantsQuota = consumeFlag(rest, "--quota");
  const refreshQuota = consumeFlag(rest, "--refresh");
  // An option-shaped token is never the provider: leave it for the leftover check so a
  // credential option keeps its operand redacted.
  const name = rest[0]?.startsWith("-") ? undefined : rest.shift();
  const leftover = leftoverArgsError(rest);
  if (leftover) {
    console.error(leftover);
    console.error(ACCOUNT_USAGE);
    return 1;
  }
  const config = (deps.loadConfigImpl?.() ?? loadConfig());
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();

  const targets: { name: string; type: AccountType; provenance: TargetProvenance }[] = [];
  if (name) {
    const c = classifyAccount(config, name);
    if ("error" in c) {
      console.error(`Error: ${c.error}. Known candidates: ${candidateNames(config)}`);
      return 1;
    }
    targets.push({ name, type: c.type, provenance: "config" });
  } else {
    const seen = new Set<string>();
    const push = (n: string, provenance: TargetProvenance) => {
      if (seen.has(n)) return;
      seen.add(n);
      const c = classifyAccount(config, n);
      if ("error" in c) return; // fan-out silently skips no-credential providers
      targets.push({ name: n, type: c.type, provenance });
    };
    push("openai", "codex");
    const providersRes = await apiJson(deps, baseUrl, "GET", "/api/oauth/providers");
    if (providersRes.status === 0) return proxyUnreachable(providersRes.transportError);
    if (providersRes.status !== 200) return apiError(providersRes.json, "failed to list OAuth providers", providersRes.status);
    if (Array.isArray(providersRes.json.providers)) {
      for (const p of providersRes.json.providers) {
        if (typeof p === "string") push(p, "live-oauth-list");
      }
    }
    for (const n of Object.keys(config.providers ?? {})) push(n, "config");
  }

  const rows: AccountRow[] = [];
  const notes: string[] = [];
  for (const t of targets) {
    const r = await fetchRows(deps, baseUrl, t.name, t.type, wantsQuota ? { refresh: refreshQuota } : undefined);
    if (r.networkDown) return proxyUnreachable(r.transportError);
    if (r.errorJson) {
      if (name) return apiError(r.errorJson, `failed to list ${t.name}`, r.status);

      const errorText = typeof r.errorJson.error === "string" ? r.errorJson.error : "";
      const skipUnknownKey = t.type === "api-key"
        && r.status === 404
        && errorText.includes("unknown provider");
      const skipConfigOAuth = t.type === "oauth"
        && t.provenance === "config"
        && r.status === 400
        && errorText.includes("unknown oauth provider");
      if (skipUnknownKey || skipConfigOAuth) continue;
      return apiError(r.errorJson, `failed to list ${t.name}`, r.status);
    }
    if (r.rows.length === 0) {
      if (showAll || name) notes.push(`${t.name}: no stored accounts or keys`, emptyAccountNextAction(t.name, t.type));
      continue;
    }
    rows.push(...r.rows);

  }

  if (rows.length === 0 && !name) notes.push("No stored accounts or keys.", emptyAccountNextAction());
  if (wantsJson) {
    console.log(JSON.stringify({ accounts: rows, notes }, null, 2));
    return 0;
  }
  if (rows.length > 0) console.log(formatAccountTable(rows, wantsQuota));
  for (const n of notes) console.log(n);
  if (rows.length === 0 && notes.length === 0) console.log("No stored accounts or keys.");
  return 0;
}

async function cmdCurrent(rest: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = consumeFlag(rest, "--json");
  // An option-shaped token is never the provider: leave it for the leftover check so a
  // credential option keeps its operand redacted.
  const name = rest[0]?.startsWith("-") ? undefined : rest.shift();
  const leftover = leftoverArgsError(rest);
  if (!name || leftover) {
    if (leftover) console.error(leftover);
    console.error(ACCOUNT_USAGE);
    return 1;
  }
  const config = (deps.loadConfigImpl?.() ?? loadConfig());
  const c = classifyAccount(config, name);
  if ("error" in c) {
    console.error(`Error: ${c.error}. Known candidates: ${candidateNames(config)}`);
    return 1;
  }
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();
  const r = await fetchRows(deps, baseUrl, name, c.type);
  if (r.networkDown) return proxyUnreachable(r.transportError);
  if (r.errorJson) return apiError(r.errorJson, `failed to read ${name}`, r.status);

  const activeRow = r.rows.find(row => row.active) ?? null;
  if (wantsJson) {
    console.log(JSON.stringify({
      provider: name,
      type: c.type,
      activeId: r.activeId,
      account: activeRow,
    }, null, 2));
    return 0;
  }
  if (activeRow) {
    console.log(formatAccountTable([activeRow]));
  } else {
    console.log(`${name}: no active account or key`);
  }
  return 0;
}

export async function cmdAccount(args: string[], deps: AccountDeps = {}): Promise<number> {
  const [sub, ...rest] = args;
  try {
    if (sub === "credits" || sub === "quota-activation" || sub === "anthropic-reset-grants") {
      const { handleAccountPolicyCommand } = await import("./account-policy");
      return await handleAccountPolicyCommand(sub, rest, deps);
    }
    if (sub === "list") return await cmdList(rest, deps);
    if (sub === "history") {
      const { cmdAccountHistory } = await import("./account-history");
      return await cmdAccountHistory(rest, deps);
    }
    if (sub === "current") return await cmdCurrent(rest, deps);
    if (sub === "refresh") return await cmdRefresh(rest, deps);
    if (sub === "alias" || sub === "rename") return await cmdAlias(rest, deps);
    // #2702: the server routes existed and only the CLI caller was missing, so these were
    // dashboard-only capabilities.
    if (sub === "pause") return await cmdPause(rest, deps, true);
    if (sub === "resume") return await cmdPause(rest, deps, false);
    if (sub === "remove") return await cmdRemove(rest, deps);
    if (sub === "clear-cooldown") return await cmdClearCooldown(rest, deps);
    if (sub === "add-key") return await cmdAddKey(rest, deps);
    if (sub === "main") {
      const { cmdNativeMainAccount } = await import("./account-main");
      return await cmdNativeMainAccount(rest, deps);
    }
    if (["login", "reauth", "code", "cancel", "reset-credits", "grok-reset-coupons"].includes(sub ?? "")) {
      const { handleAccountAuthCommand } = await import("./account-auth");
      return await handleAccountAuthCommand(sub!, rest, deps) ?? 1;
    }
    console.error(ACCOUNT_USAGE);
    return 1;
  } catch (err) {
    console.error(`account: ${err instanceof Error ? err.message : String(err)}`);
    return 1;
  }
}
