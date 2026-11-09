import { isAnthropicOAuthInstance } from "../providers/anthropic-instance";
import { loadConfig } from "../config";
import { isReservedCodexAccountWord, reportCodexAccountTargetError, resolveCodexAccountTarget } from "./account-target";
import { hasPassiveAccountQuota } from "../providers/quota";
import {
  apiError,
  apiJson,
  classifyAccount,
  fetchCodexRows,
  fetchProviderQuotaReport,
  fetchRows,
  proxyUnreachable,
  resolveBaseUrl,
  type AccountDeps, type AccountStdin, type FamilyRows,
  type ProviderQuotaDto, type ProviderQuotaReportDto,
} from "./account-api";
import {
  codexCatalogRefreshPending,
  warnIfCodexCatalogRefreshPending,
} from "./account-catalog-refresh";
const MAIN_ID = "__main__";
const EXTENDED_USAGE = `Usage:
  ocx account refresh <provider> [--json]
  ocx account alias <provider> <id|alias|main> <display-name|-> [--json]
  ocx account pause <provider> <id|alias|main> [--json]
  ocx account resume <provider> <id|alias|main> [--json]
  ocx account remove <provider> <id|alias|main> --yes [--json]
  ocx account clear-cooldown <provider> <id|alias|main> [--json]
  ocx account add-key <provider> [--label <label>] [--json]`;
const PIPE_GUIDANCE = `Pipe the API key on stdin, for example:
  printf '%s\\n' "$MY_KEY" | ocx account add-key <provider>`;

function flag(args: string[], value: string): boolean {
  const index = args.indexOf(value);
  if (index < 0) return false;
  args.splice(index, 1);
  return true;
}

function flagValue(args: string[], value: string): { found: boolean; value?: string } {
  const index = args.indexOf(value);
  if (index < 0) return { found: false };
  if (index + 1 >= args.length) return { found: true };
  const result = args[index + 1];
  args.splice(index, 2);
  return { found: true, value: result };
}

function usage(message?: string): number {
  if (message) console.error(message);
  console.error(EXTENDED_USAGE);
  return 1;
}

function configAndType(deps: AccountDeps, name: string) {
  return classifyAccount(deps.loadConfigImpl?.() ?? loadConfig(), name);
}

function familyFailure(result: FamilyRows, fallback: string): number | null {
  if (result.networkDown) return proxyUnreachable(result.transportError);
  if (result.errorJson) return apiError(result.errorJson, fallback, result.status);
  return null;
}

function errorText(json: Record<string, unknown> | undefined, fallback: string): string {
  return typeof json?.error === "string" ? json.error : fallback;
}

function resetIso(value: number | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const date = new Date(value < 10_000_000_000 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function refreshLine(row: FamilyRows["rows"][number]): string {
  const parts = [row.id === MAIN_ID ? "main" : row.id, row.email, row.plan];
  if (row.paused) parts.push(row.pauseDetails ? `paused(${row.pauseDetails.reason})` : "paused");
  // Was a second quota dialect: it gated the whole block on weekly/monthly, so an account
  // reporting only a 5h window printed `quota: unknown` while `quotaParts` five lines below
  // rendered the same data correctly for the provider path (#2703). Two halves of one file
  // disagreeing about how to read one DTO is the defect; delegating removes it rather than
  // teaching the second dialect a third window.
  const quotaText = row.quota ? quotaParts(row.quota).join(" ") : "";
  parts.push(quotaText.length > 0 ? quotaText : "quota: unknown");
  if (row.needsReauth) parts.push("needs-reauth");
  if (row.validationPending) parts.push("validation-pending (routing disabled; open 'ocx gui' and click Refresh quotas after recovery)");
  return parts.filter(Boolean).join(" ");
}

function quotaParts(quota: ProviderQuotaDto): string[] {
  const parts: string[] = [];
  const add = (label: string, percent: number | undefined, resetAt?: number) => {
    if (percent === undefined) return;
    parts.push(`${label} ${percent}%`);
    const reset = resetIso(resetAt);
    if (reset) parts.push(`resets ${reset}`);
  };
  add("5h", quota.fiveHourPercent, quota.fiveHourResetAt);
  add("weekly", quota.weeklyPercent, quota.weeklyResetAt);
  add("monthly", quota.monthlyPercent, quota.monthlyResetAt);
  for (const window of quota.customWindows ?? []) add(window.label, window.percent, window.resetAt);
  return parts;
}

export function providerQuotaLine(name: string, report: ProviderQuotaReportDto): string {
  return [name, ...quotaParts(report.quota)].join(" ");
}

export async function readStdinLine(deps: AccountDeps): Promise<string> {
  const input: AccountStdin = deps.stdinImpl ?? process.stdin;
  const timeoutMs = deps.stdinTimeoutMs ?? 15_000;
  return await new Promise<string>((resolve, reject) => {
    let buffer = "";
    let settled = false;
    const cleanup = () => {
      clearTimeout(timer);
      input.removeListener("data", onData);
      input.removeListener("end", onEnd);
      input.removeListener("error", onError);
    };
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn();
    };
    const onData = (chunk: unknown) => {
      buffer += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
      const newline = buffer.search(/[\r\n]/);
      if (newline >= 0) finish(() => resolve(buffer.slice(0, newline).trim()));
    };
    const onEnd = () => finish(() => resolve(buffer.trim()));
    const onError = (error: Error) => finish(() => reject(error));
    const timer = setTimeout(() => finish(() => reject(new Error("timed out waiting for API key on stdin"))), timeoutMs);
    input.on("data", onData);
    input.on("end", onEnd);
    input.on("error", onError);
  });
}

export async function cmdRefresh(args: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = flag(args, "--json");
  const name = args.shift();
  if (!name || args.length) return usage();
  const classified = configAndType(deps, name);
  if ("error" in classified) return usage(`Error: ${classified.error}`);
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();
  if (classified.type !== "codex") {
    const result = await fetchProviderQuotaReport(deps, baseUrl, name);
    if (result.status === 0) return proxyUnreachable(result.transportError);
    if (result.status !== 200) return apiError(result.errorJson ?? {}, `failed to refresh ${name}`, result.status);
    if (wantsJson) console.log(JSON.stringify({ provider: name, report: result.report }, null, 2));
    else if (result.report) console.log(providerQuotaLine(name, result.report));
    // A passive provider has no probe to run, so "no report available" reads as a
    // failure of something that was never attempted. Say what is actually true.
    else if (hasPassiveAccountQuota(name)) {
      console.log(`${name} reports usage only during a streaming response; there is nothing to refresh. Run a request through this provider to update it, then see \`ocx account list ${name}\`.`);
    } else console.log(`no quota report available for ${name}`);
    return 0;
  }
  const result = await fetchCodexRows(deps, baseUrl, true, true, { refreshAction: true });
  const failed = familyFailure(result, `failed to refresh ${name}`);
  if (failed !== null) return failed;
  if (wantsJson) console.log(JSON.stringify({ accounts: result.rows }, null, 2));
  else for (const row of result.rows) console.log(refreshLine(row));
  return 0;
}

function deletePath(type: "codex" | "oauth" | "api-key", name: string, id: string): string {
  if (type === "codex") return `/api/codex-auth/accounts?id=${encodeURIComponent(id)}`;
  if (type === "oauth") return `/api/oauth/accounts?provider=${encodeURIComponent(name)}&id=${encodeURIComponent(id)}`;
  return `/api/providers/keys?name=${encodeURIComponent(name)}&id=${encodeURIComponent(id)}`;
}

export async function cmdRemove(args: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = flag(args, "--json");
  const fail = (message: string): number => {
    if (wantsJson) console.error(JSON.stringify({ error: message }));
    else console.error(`Error: ${message}`);
    return 1;
  };
  const confirmed = flag(args, "--yes");
  const name = args.shift();
  const requestedId = args.shift();
  if (!name || !requestedId || args.length) return wantsJson ? fail("provider and account id are required") : usage();
  if (!confirmed) {
    const message = `Confirmation required. Re-run: ocx account remove ${name} ${requestedId} --yes`;
    return wantsJson ? fail(message) : usage(message);
  }
  const classified = configAndType(deps, name);
  if ("error" in classified) return wantsJson ? fail(classified.error) : usage(`Error: ${classified.error}`);
  if (classified.type === "codex" && (requestedId === "main" || requestedId === MAIN_ID)) return wantsJson
    ? fail("the main Codex App login cannot be removed")
    : usage("Error: the main Codex App login cannot be removed");
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return fail("Proxy not reachable. Start it with 'ocx start' or 'ocx ensure'.");
  let id = requestedId;
  if (classified.type === "codex") {
    const target = await resolveCodexAccountTarget(deps, baseUrl, requestedId);
    if ("networkDown" in target) {
      const reason = target.transportError ? ` reason: ${target.transportError}` : "";
      return fail(`Proxy not reachable. Start it with 'ocx start' or 'ocx ensure'.${reason}`);
    }
    if ("error" in target) {
      const message = target.kind === "not_found" ? `account or key "${requestedId}" was not found` : target.error;
      return wantsJson ? fail(message) : usage(`Error: ${message}`);
    }
    id = target.id;
  }
  const before = await fetchRows(deps, baseUrl, name, classified.type);
  if (before.networkDown) return fail("Proxy not reachable. Start it with 'ocx start' or 'ocx ensure'.");
  if (before.errorJson) return fail(errorText(before.errorJson, `failed to verify ${name} before removal`));
  if (!before.rows.some(row => row.id === id)) return wantsJson
    ? fail(`account or key "${requestedId}" was not found`)
    : usage(`Error: account or key "${requestedId}" was not found`);
  const response = await apiJson(deps, baseUrl, "DELETE", deletePath(classified.type, name, id));
  if (response.status === 0) return fail("Proxy not reachable. Start it with 'ocx start' or 'ocx ensure'.");
  if (response.status !== 200) return fail(errorText(response.json, `failed to remove ${requestedId}`));
  const catalogRefreshPending = classified.type === "codex"
    && codexCatalogRefreshPending(response.json);
  const after = await fetchRows(deps, baseUrl, name, classified.type);
  if (after.networkDown || after.errorJson) {
    const detail = after.networkDown ? "proxy not reachable" : typeof after.errorJson?.error === "string" ? after.errorJson.error : "unknown error";
    return fail(`post-delete verification failed; delete may have succeeded: ${detail}`);
  }
  const removedActive = before.activeId === id;
  const result = {
    ok: true,
    provider: name,
    id,
    removedActive,
    promotedActiveId: after.activeId,
    ...(classified.type === "codex" ? { catalogRefreshPending } : {}),
  };
  if (wantsJson) console.log(JSON.stringify(result, null, 2));
  else if (classified.type === "codex" && removedActive && after.activeId === null) console.log("openai: no active account");
  else if (classified.type === "oauth") console.log(after.rows.length ? `${name}: active account is now ${after.activeId}` : `${name}: no accounts remaining`);
  else if (classified.type === "api-key") console.log(after.rows.length ? `${name}: active key is now ${after.activeId}` : `${name}: no keys remaining`);
  else console.log(`${name}: removed account ${requestedId}`);
  if (!wantsJson && catalogRefreshPending) warnIfCodexCatalogRefreshPending(response.json);
  return 0;
}

export async function cmdAddKey(args: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = flag(args, "--json");
  const labelArg = flagValue(args, "--label");
  const name = args.shift();
  if (!name || args.length || (labelArg.found && labelArg.value === undefined)) return usage();
  const classified = configAndType(deps, name);
  if ("error" in classified || classified.type !== "api-key") return usage("Error: add-key only applies to API-key providers");
  const input: AccountStdin = deps.stdinImpl ?? process.stdin;
  if (input.isTTY) return usage(PIPE_GUIDANCE);
  let key: string;
  try {
    key = await readStdinLine(deps);
  } catch (error) {
    return usage(`Error: ${error instanceof Error ? error.message : String(error)}\n${PIPE_GUIDANCE}`);
  }
  if (!key) return usage(`Error: API key input was empty\n${PIPE_GUIDANCE}`);
  const label = labelArg.value?.trim();
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();
  const response = await apiJson(deps, baseUrl, "POST", "/api/providers/keys", { name, key, ...(label ? { label } : {}) });
  if (response.status === 0) return proxyUnreachable(response.transportError);
  if (response.status !== 201) return apiError(response.json, `failed to add a key for ${name}`, response.status);
  const id = typeof response.json.id === "string" ? response.json.id : null;
  // Redact the key inside the label BEFORE serialization — a key containing
  // JSON-escaped characters (" or \) would otherwise survive the whole-output
  // pass in escaped form (audit finding, Carver WP3-C).
  const safeLabel = label ? label.replaceAll(key, "[redacted]") : undefined;
  const result = { ok: true, id, ...(safeLabel ? { label: safeLabel } : {}) };
  const output = wantsJson ? JSON.stringify(result, null, 2)
    : `${name}: added API key ${id ?? ""}${safeLabel ? ` (${safeLabel})` : ""}`.trim();
  console.log(output.replaceAll(key, "[redacted]"));
  return 0;
}

export async function cmdClearCooldown(args: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = flag(args, "--json");
  const name = args.shift();
  const requestedId = args.shift();
  if (!name || !requestedId || args.length) return usage();
  const classified = configAndType(deps, name);
  if ("error" in classified) return usage(`Error: ${classified.error}`);
  if (classified.type !== "codex" && !(classified.type === "oauth" && isAnthropicOAuthInstance(name))) {
    return usage(`Error: ${name} has no operator-clearable account cooldown`);
  }
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();
  let id: string;
  let response: Awaited<ReturnType<typeof apiJson>>;
  if (classified.type === "codex") {
    const target = await resolveCodexAccountTarget(deps, baseUrl, requestedId);
    if ("networkDown" in target) return proxyUnreachable(target.transportError);
    if ("error" in target) return reportCodexAccountTargetError(target);
    id = target.id;
    response = await apiJson(deps, baseUrl, "POST", "/api/codex-auth/accounts/clear-cooldown", { id });
  } else {
    const list = await apiJson(deps, baseUrl, "GET", `/api/oauth/accounts?provider=${encodeURIComponent(name)}`);
    if (list.status === 0) return proxyUnreachable(list.transportError);
    if (list.status !== 200) return apiError(list.json, `failed to list ${name} OAuth accounts`, list.status);
    const target = resolveGenericOAuthAccountTarget(
      Array.isArray(list.json.accounts) ? list.json.accounts : [],
      requestedId,
    );
    if ("error" in target) return usage(`Error: ${target.error}`);
    id = target.id;
    response = await apiJson(deps, baseUrl, "POST", "/api/oauth/accounts/clear-cooldown", {
      provider: name,
      accountId: id,
    });
  }
  if (response.status === 0) return proxyUnreachable(response.transportError);
  if (response.status !== 200) return apiError(response.json, `failed to clear cooldown for ${requestedId}`, response.status);
  const cleared = response.json?.cleared === true;
  if (wantsJson) console.log(JSON.stringify({ ok: true, provider: name, id, cleared }, null, 2));
  else if (cleared) console.log(`${name}: cooldown lifted for ${requestedId}`);
  else console.log(`${name}: no active cooldown for ${requestedId}`);
  return 0;
}

/**
 * Named selection orders. The words convey sequence rather than rank because the
 * pool moves down the list only when everything above it is drained — "high
 * priority" would suggest the account gets more traffic, which is not what
 * ordering does.
 */
function resolveGenericOAuthAccountTarget(accounts: unknown[], requested: string, kind: "OAuth" | "API key" = "OAuth"): { id: string } | { error: string } {
  const rows = accounts.filter((value): value is { id: string; alias?: unknown } =>
    typeof value === "object" && value !== null && typeof (value as { id?: unknown }).id === "string",
  );
  if (rows.some(account => account.id === requested)) return { id: requested };
  const exact = rows.filter(account => account.alias === requested);
  const matches = exact.length > 0
    ? exact
    : rows.filter(account => typeof account.alias === "string" && account.alias.toLowerCase() === requested.toLowerCase());
  if (matches.length === 1) return { id: matches[0]!.id };
  if (matches.length > 1) return { error: `alias "${requested}" names ${matches.length} accounts; use the account id` };
  return { error: `Account not found: no ${kind} account has the id or alias "${requested}"` };
}

/** Pause or resume a Codex, OAuth, or API-key pool account. */
export async function cmdPause(args: string[], deps: AccountDeps, paused: boolean): Promise<number> {
  const wantsJson = flag(args, "--json");
  const name = args.shift();
  const requestedId = args.shift();
  const verb = paused ? "pause" : "resume";
  if (!name || !requestedId || args.length) return usage();
  const classified = configAndType(deps, name);
  if ("error" in classified) return usage(`Error: ${classified.error}`);
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();

  if (classified.type === "oauth") {
    const list = await apiJson(deps, baseUrl, "GET", `/api/oauth/accounts?provider=${encodeURIComponent(name)}`);
    if (list.status === 0) return proxyUnreachable(list.transportError);
    if (list.status !== 200) return apiError(list.json, `failed to list ${name} OAuth accounts`, list.status);
    const target = resolveGenericOAuthAccountTarget(Array.isArray(list.json.accounts) ? list.json.accounts : [], requestedId);
    if ("error" in target) return usage(`Error: ${target.error}`);

    const response = await apiJson(deps, baseUrl, "PUT", "/api/oauth/accounts/pause", {
      provider: name,
      accountId: target.id,
      paused,
    });
    if (response.status === 0) return proxyUnreachable(response.transportError);
    if (response.status !== 200) return apiError(response.json, `failed to ${verb} ${requestedId}`, response.status);

    if (wantsJson) {
      console.log(JSON.stringify({ ok: true, provider: name, id: target.id, paused,
        activeAccountId: response.json.activeAccountId }, null, 2));
    } else {
      console.log(`${name}: ${requestedId} ${paused ? "paused" : "resumed"}`);
      if (response.json.activeAccountChanged === true) {
        console.error(`Active account changed to ${String(response.json.activeAccountId)}.`);
      }
    }
    return 0;
  }

  if (classified.type === "api-key") {
    const rows = await fetchRows(deps, baseUrl, name, "api-key");
    if (rows.status === 0) return proxyUnreachable(rows.transportError);
    if (rows.status !== 200) return apiError(rows.errorJson ?? {}, "failed to list API key accounts", rows.status);
    const target = resolveGenericOAuthAccountTarget(rows.rows.map(row => ({ id: row.id, alias: row.label })), requestedId, "API key");
    if ("error" in target) return usage(`Error: ${target.error}`);
    const result = await apiJson(deps, baseUrl, "PUT", "/api/providers/keys/pause", { name, id: target.id, paused });
    if (result.status === 0) return proxyUnreachable(result.transportError);
    if (result.status !== 200) return apiError(result.json, `failed to ${verb} account`, result.status);
    console.log(wantsJson ? JSON.stringify({ ok: true, provider: name, id: target.id, paused }, null, 2) : `${name}: account ${paused ? "paused" : "resumed"}`);
    return 0;
  }
  if (classified.type !== "codex") {
    return usage(`Error: ${verb} applies to the openai Codex account pool, a generic OAuth provider, or an API-key pool`);
  }
  const target = await resolveCodexAccountTarget(deps, baseUrl, requestedId);
  if ("networkDown" in target) return proxyUnreachable(target.transportError);
  if ("error" in target) return reportCodexAccountTargetError(target);
  const id = target.id;

  const response = await apiJson(deps, baseUrl, "PUT", "/api/codex-auth/accounts/pause", { id, paused });
  // Transport sentinel first: status 0 means the proxy never answered, and comparing it to
  // 200 would report an unreachable proxy as a management error.
  if (response.status === 0) return proxyUnreachable(response.transportError);
  if (response.status !== 200) return apiError(response.json, `failed to ${verb} ${requestedId}`, response.status);

  if (wantsJson) {
    console.log(JSON.stringify({ ok: true, provider: name, id, paused }, null, 2));
  } else {
    console.log(`${name}: ${requestedId} ${paused ? "paused" : "resumed"}`);
  }
  if (paused) {
    // Both are server-side effects of this route (auth-api.ts:1508-1510), not consequences
    // the operator would infer from the word "pause".
    console.error("Threads bound to this account are unbound, and a fallback account is selected if this one was active.");
  }
  return 0;
}

/**
 * `ocx account pause-exhausted [--off]` (#2702).
 *
 * Pauses every account whose quota is spent. The route refreshes quota for each account, so
 * it can partially fail; the response distinguishes "checked none and some failed" from
 * "checked some", and that distinction is reported rather than flattened into ok/not-ok.
 */
export async function cmdAlias(args: string[], deps: AccountDeps): Promise<number> {
  const wantsJson = flag(args, "--json");
  const name = args.shift();
  const requestedId = args.shift();
  const requestedAlias = args.shift();
  if (!name || !requestedId || requestedAlias === undefined || args.length) return usage();
  const classified = configAndType(deps, name);
  if ("error" in classified) return usage(`Error: ${classified.error}`);
  if (classified.type === "codex" && (requestedId === "main" || requestedId === MAIN_ID)) {
    return usage("Error: the main Codex App login cannot be renamed");
  }
  const alias = requestedAlias === "-" ? "" : requestedAlias.trim();
  if (alias.length > 80 || /[\x00-\x1f\x7f]/.test(alias)) return usage("Error: alias must be at most 80 printable characters");
  if (classified.type === "codex" && isReservedCodexAccountWord(alias)) return usage(`Error: "${alias}" is reserved for \`ocx account use\` and cannot be an alias`);
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();
  let id = requestedId;
  if (classified.type === "codex") {
    const target = await resolveCodexAccountTarget(deps, baseUrl, requestedId);
    if ("networkDown" in target) return proxyUnreachable(target.transportError);
    if ("error" in target) return reportCodexAccountTargetError(target);
    id = target.id;
  }
  const path = classified.type === "codex"
    ? "/api/codex-auth/accounts/alias"
    : classified.type === "oauth"
      ? "/api/oauth/accounts/alias"
      : "/api/providers/keys/alias";
  const body = classified.type === "codex"
    ? { id, alias }
    : classified.type === "oauth"
      ? { provider: name, accountId: id, alias }
      : { name, id, alias };
  const response = await apiJson(deps, baseUrl, "PUT", path, body);
  if (response.status === 0) return proxyUnreachable(response.transportError);
  if (response.status !== 200) return apiError(response.json, `failed to rename ${requestedId}`, response.status);
  const result = { ok: true, provider: name, id, alias: alias || null };
  if (wantsJson) console.log(JSON.stringify(result, null, 2));
  else console.log(alias ? `${name}: ${requestedId} is now “${alias}”` : `${name}: cleared alias for ${requestedId}`);
  return 0;
}
