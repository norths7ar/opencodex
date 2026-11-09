

import { CODEX_ACCOUNT_LOG_LABEL_RE, codexAccountLogLabel } from "../account-label";

import { poolQuotaHistoryIdentity } from "../account-store";
import { estimateCodexQuotaCapacity, insufficientCodexCapacity } from "../quota-capacity";
import type { CodexCapacityResult } from "../quota-capacity";
import { readUsageSnapshotForManagement } from "../../usage/log";
import { getAccountQuotaHistory, listAccountQuotas } from "../quota";
import { deleteCodexAccount } from "../account-lifecycle";
import { setCodexAccountPaused } from "../account-pause";
import { setAllCodexAccountsCreditsAfterLimit, setCodexAccountCreditsAfterLimit } from "../account-credit-use";
import { isCodexAccountPriorityKey, pinnedCodexAccountId } from "../account-priority";
import { codexQuotaScopeForModel, clearCodexAccountCooldown, clearThreadAccountMapForAccount, getEffectiveActiveCodexAccountId, isEffectiveCodexAccountPinned } from "../routing";
import { normalizeAccountPoolStickyLimit, normalizeCodexAccountPoolStrategy } from "../pool-rotation";
import { MAIN_CODEX_ACCOUNT_ID } from "../main-account";
import { reconcileLiveStateStores } from "../../lib/state-store-registrations";
import type { OcxConfig } from "../../types";
import { CODEX_ACCOUNT_ID_RE, isSelectableCodexPoolAccount, isValidCodexAccountId } from "../account-id";
import { isCodexResetCreditOperationId } from "../reset-credit-recovery";
import { listCodexAuthAccounts, selectFallbackAfterPause } from "./account-list";
import { jsonResponse, manualImportDisabledResponse } from "./http";
import { convergeAccountNamespaceCatalog, handleCodexAuthLoginStart, handleCodexAuthLoginCode, handleCodexAuthLoginCancel, handleCodexAuthLoginStatus } from "./login-flow";
import type { CodexAuthCatalogConvergence } from "./login-flow";
import { PoolQuotaProbeBusyError } from "./pool-quota-probe";
import { inspectResetCredits, consumeResetCredits } from "./reset-credit-service";
import { getRuntimeConfig, saveRuntimeConfig, configuredPoolAccount } from "./runtime-config";


import { withCodexAccountPauseGroup } from "./account-pause-group";

export async function handleCodexAuthAPI(
  req: Request,
  url: URL,
  config: OcxConfig,
  convergeCodexCatalog?: CodexAuthCatalogConvergence,
  principal?: import("../../server/management-auth").ManagementPrincipal,
): Promise<Response | null> {
  if (["/api/codex-auth/preserve-signed-in","/api/codex-auth/accounts/use-remaining","/api/codex-auth/accounts/priority","/api/codex-auth/accounts/pause-exhausted","/api/codex-auth/auto-switch","/api/codex-auth/pool-strategy","/api/codex-auth/failover"].includes(url.pathname)) {
    return jsonResponse({ error: "Account pools are not supported" }, 410);
  }
  const operation = req.method + " " + url.pathname;
  if (operation === "POST /api/codex-auth/accounts") return manualImportDisabledResponse();
  if (operation === "PUT /api/codex-auth/active") {
    return jsonResponse({ error: "Account switching is not supported; sign in to replace this channel account" }, 410);
  }
  if (url.pathname === "/api/codex-auth/accounts" && req.method === "GET") {
    const forceRefresh = url.searchParams.get("refresh") === "1" || url.searchParams.get("refresh") === "true";
    return jsonResponse({ accounts: await listCodexAuthAccounts(config, forceRefresh) });
  }

  if (url.pathname === "/api/codex-auth/accounts/refresh" && req.method === "POST") {
    // Inference spends quota: only a dashboard session carries the consent
    // for this operation. Raw-admin/CLI refreshes remain observational.
    return jsonResponse({ accounts: await listCodexAuthAccounts(config, true, {
      validatePending: principal === "gui-session",
      explicitRefresh: true,
    }) });
  }

  if (url.pathname === "/api/codex-auth/accounts" && req.method === "DELETE") {
    const id = url.searchParams.get("id");
    if (!id) return jsonResponse({ error: "Missing id" }, 400);
    const runtimeConfig = getRuntimeConfig(config);
    const isLegacyPoolAccount = CODEX_ACCOUNT_ID_RE.test(id)
      && (runtimeConfig.codexAccounts ?? []).some(account => !account.isMain && account.id === id);
    if (!isValidCodexAccountId(id) && !isLegacyPoolAccount) {
      return jsonResponse({ error: "Invalid account id format" }, 400);
    }
    const pickerVisibilityChanged = deleteCodexAccount(runtimeConfig, id);
    saveRuntimeConfig(config, runtimeConfig);
    reconcileLiveStateStores();
    const catalogRefresh = await convergeAccountNamespaceCatalog(
      runtimeConfig,
      pickerVisibilityChanged,
      convergeCodexCatalog,
    );
    return jsonResponse({ ok: true, ...catalogRefresh });
  }

  if (url.pathname === "/api/codex-auth/accounts/alias" && req.method === "PUT") {
    const body = await req.json().catch(() => ({})) as { id?: unknown; alias?: unknown };
    const id = typeof body.id === "string" ? body.id.trim() : "";
    const alias = typeof body.alias === "string" ? body.alias.trim() : "";
    if (id === MAIN_CODEX_ACCOUNT_ID) return jsonResponse({ error: "Main Codex account alias is not configurable" }, 400);
    if (!isValidCodexAccountId(id)) return jsonResponse({ error: "Invalid account id format" }, 400);
    if (typeof body.alias !== "string" || alias.length > 80 || /[\x00-\x1f\x7f]/.test(alias)) {
      return jsonResponse({ error: "Alias must be a string of at most 80 printable characters" }, 400);
    }
    const runtimeConfig = getRuntimeConfig(config);
    const account = (runtimeConfig.codexAccounts ?? []).find(candidate => candidate.id === id && !candidate.isMain);
    if (!account) return jsonResponse({ error: "Account not found" }, 404);
    if (alias) account.alias = alias;
    else delete account.alias;
    saveRuntimeConfig(config, runtimeConfig);
    return jsonResponse({ ok: true, id, alias: alias || null });
  }

  if (url.pathname === "/api/codex-auth/accounts/pause" && req.method === "PUT") {
    const body = await req.json().catch(() => ({})) as { id?: unknown; paused?: unknown };
    const id = typeof body.id === "string" ? body.id.trim() : "";
    if (id !== MAIN_CODEX_ACCOUNT_ID && !isValidCodexAccountId(id)) {
      return jsonResponse({ error: "Invalid account id format" }, 400);
    }
    if (typeof body.paused !== "boolean") return jsonResponse({ error: "paused must be a boolean" }, 400);

    const runtimeConfig = getRuntimeConfig(config);
    const exists = id === MAIN_CODEX_ACCOUNT_ID
      || (runtimeConfig.codexAccounts ?? []).some(account => isSelectableCodexPoolAccount(account) && account.id === id);
    if (!exists) return jsonResponse({ error: "Account not found" }, 404);

    const paused = body.paused;
    return withCodexAccountPauseGroup(runtimeConfig, id, accountIds => {
      // Publish every exclusion before reconciling: a duplicate must never become the fallback.
      for (const accountId of accountIds) setCodexAccountPaused(runtimeConfig, accountId, paused);
      if (paused) {
        for (const accountId of accountIds) clearThreadAccountMapForAccount(accountId);
        for (const accountId of accountIds) selectFallbackAfterPause(runtimeConfig, accountId);
      }
      saveRuntimeConfig(config, runtimeConfig);
      return jsonResponse({
        ok: true,
        id,
        paused,
        affectedAccountIds: accountIds,
        activeCodexAccountId: runtimeConfig.codexSignedOut ? null : getEffectiveActiveCodexAccountId(runtimeConfig) ?? null,
        appliesImmediately: true,
      });
    });
  }

  // Pool accounts and `__main__`, like pause. Spending is opt-in: an account not on the list is
  // skipped by the next selection while a usage window is full, and a bound thread is released
  // through the usual block reason, so nothing is cleared here. `{ all }` is the dashboard's
  // global switch: on lists every current account, off clears the list.
  if (url.pathname === "/api/codex-auth/accounts/credits" && req.method === "PUT") {
    const parsed: unknown = await req.json().catch(() => null);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return jsonResponse({ error: "body must be an object" }, 400);
    }
    const body = parsed as { id?: unknown; creditsAfterLimit?: unknown; all?: unknown };
    const runtimeConfig = getRuntimeConfig(config);
    if (body.all !== undefined) {
      if (typeof body.all !== "boolean") return jsonResponse({ error: "all must be a boolean" }, 400);
      const ids = [MAIN_CODEX_ACCOUNT_ID, ...(runtimeConfig.codexAccounts ?? [])
        .filter(isSelectableCodexPoolAccount)
        .map(account => account.id)];
      setAllCodexAccountsCreditsAfterLimit(runtimeConfig, ids, body.all);
      saveRuntimeConfig(config, runtimeConfig);
      return jsonResponse({ ok: true, all: body.all, ids: runtimeConfig.creditCodexAccountIds ?? [] });
    }
    const id = typeof body.id === "string" ? body.id.trim() : "";
    if (id !== MAIN_CODEX_ACCOUNT_ID && !isValidCodexAccountId(id)) return jsonResponse({ error: "Invalid account id format" }, 400);
    if (typeof body.creditsAfterLimit !== "boolean") return jsonResponse({ error: "creditsAfterLimit must be a boolean" }, 400);

    const exists = id === MAIN_CODEX_ACCOUNT_ID
      || (runtimeConfig.codexAccounts ?? []).some(account => isSelectableCodexPoolAccount(account) && account.id === id);
    if (!exists) return jsonResponse({ error: "Account not found" }, 404);

    setCodexAccountCreditsAfterLimit(runtimeConfig, id, body.creditsAfterLimit);
    saveRuntimeConfig(config, runtimeConfig);
    return jsonResponse({ ok: true, id, creditsAfterLimit: body.creditsAfterLimit });
  }

  // Manual escape from a quota cooldown. Injected Codex routing makes this proxy the only
  // model path for Codex Desktop, so a cooldown that outlives the real upstream limit
  // otherwise leaves editing config.toml as the user's only recovery.
  //
  // Existence is deliberately NOT disclosed: an unknown id returns 200 with cleared:false
  // exactly like an account that simply had no live cooldown, so this route cannot be used
  // to enumerate configured accounts. Cooldown state is runtime-only and independent of the
  // account list, so 404 would carry no useful meaning anyway.
  if (url.pathname === "/api/codex-auth/accounts/clear-cooldown" && req.method === "POST") {
    const body = await req.json().catch(() => ({})) as { id?: unknown };
    const id = typeof body.id === "string" ? body.id.trim() : "";
    if (id !== MAIN_CODEX_ACCOUNT_ID && !isValidCodexAccountId(id)) {
      return jsonResponse({ error: "Invalid account id format" }, 400);
    }
    return jsonResponse({ ok: true, id, cleared: clearCodexAccountCooldown(id) });
  }

  if (url.pathname === "/api/codex-auth/active" && req.method === "GET") {
    const runtimeConfig = getRuntimeConfig(config);
    return jsonResponse({
      activeCodexAccountId: runtimeConfig.codexSignedOut ? null : getEffectiveActiveCodexAccountId(runtimeConfig) ?? null,
      pinned: isEffectiveCodexAccountPinned(runtimeConfig),
      // Which account carries the pin, not just whether the active one does. Under
      // round-robin or fill-first the pin caps the tier ceiling at its own tier while the
      // strategy cursor moves freely inside that tier, so `pinned` alone goes false on a
      // sibling's turn even though the pin is still suppressing every higher tier. The id
      // lets a surface mark the account the operator actually chose.
      pinnedAccountId: pinnedCodexAccountId(runtimeConfig) ?? null,
      autoSwitchThreshold: runtimeConfig.autoSwitchThreshold ?? 80,
      upstreamFailoverThreshold: runtimeConfig.upstreamFailoverThreshold ?? 3,
      accountPoolStrategy: normalizeCodexAccountPoolStrategy(runtimeConfig.accountPoolStrategy),
      accountPoolStickyLimit: normalizeAccountPoolStickyLimit(runtimeConfig.accountPoolStickyLimit),
    });
  }

  if (url.pathname === "/api/codex-auth/quota/history" && req.method === "GET") {
    const accountId = url.searchParams.get("accountId");
    const rawLimit = url.searchParams.get("limit");
    if (url.searchParams.getAll("accountId").length !== 1 || !isValidCodexAccountId(accountId)
      || url.searchParams.getAll("limit").length > 1
      || [...url.searchParams.keys()].some(key => key !== "accountId" && key !== "limit")
      || (rawLimit !== null && !/^(?:[1-9]|[1-9][0-9]|1[0-9]{2}|200)$/.test(rawLimit))) {
      return jsonResponse({ error: "A stored pool accountId and optional limit from 1 to 200 are required" }, 400);
    }
    const runtimeConfig = getRuntimeConfig(config);
    const account = configuredPoolAccount(runtimeConfig, accountId);
    if (!account) return jsonResponse({ error: "Unknown pool account" }, 404);
    const identity = poolQuotaHistoryIdentity(accountId);
    const allHistory = getAccountQuotaHistory(accountId);
    const limit = rawLimit === null ? 200 : Number(rawLimit);
    const history = { ...allHistory, observations: allHistory.observations.slice(-limit), truncated: allHistory.observations.length > limit };
    const label = account.logLabel;
    const labelStillUnique = () => {
      const current = getRuntimeConfig(config);
      return configuredPoolAccount(current, accountId)?.logLabel === label
        && current.codexAccounts?.filter(row => codexAccountLogLabel(row) === label).length === 1;
    };
    let capacity: CodexCapacityResult = insufficientCodexCapacity("identity_unavailable");
    if (identity && identity === poolQuotaHistoryIdentity(accountId) && label && CODEX_ACCOUNT_LOG_LABEL_RE.test(label) && labelStillUnique()) {
      try {
        const usage = await readUsageSnapshotForManagement();
        if (poolQuotaHistoryIdentity(accountId) !== identity || !labelStillUnique()) capacity = insufficientCodexCapacity("identity_changed");
        else if (!usage.revision) capacity = insufficientCodexCapacity("ledger_unavailable");
        else if (usage.truncatedPrefixBytes > 0 || usage.entriesTruncated || usage.entriesDropped > 0) capacity = insufficientCodexCapacity("ledger_truncated");
        else capacity = estimateCodexQuotaCapacity(allHistory.observations, usage.entries, label,
          model => codexQuotaScopeForModel(model) === "shared");
      } catch { capacity = insufficientCodexCapacity("ledger_unavailable"); }
    }
    if (!configuredPoolAccount(getRuntimeConfig(config), accountId)) return jsonResponse({ error: "Unknown pool account" }, 404);
    if (identity !== poolQuotaHistoryIdentity(accountId) || (identity && label && !labelStillUnique())) {
      return jsonResponse({ accountId, ...getAccountQuotaHistory(accountId, limit), capacity: insufficientCodexCapacity("identity_changed") });
    }
    return jsonResponse({ accountId, ...history, capacity });
  }

  if (url.pathname === "/api/codex-auth/quota" && req.method === "GET") {
    const quotas: Record<string, unknown> = {};
    for (const [id, q] of listAccountQuotas()) quotas[id] = q;
    return jsonResponse({ quotas });
  }

  if (url.pathname === "/api/codex-auth/reset-credits" && req.method === "GET") {
    const accountId = url.searchParams.get("accountId");
    if (!accountId) return jsonResponse({ error: "accountId required" }, 400);

    try {
      return await inspectResetCredits(config, accountId, req.signal);
    } catch (e) {
      return jsonResponse({ error: e instanceof Error ? e.message : "Reset credit lookup failed" }, 500);
    }
  }

  if (url.pathname === "/api/codex-auth/reset-credits/consume" && req.method === "POST") {
    const body = (await req.json().catch(() => ({}))) as {
      accountId?: string;
      operationId?: unknown;
    };
    if (!body.accountId) return jsonResponse({ error: "accountId required" }, 400);
    const accountId = body.accountId;
    // Optional caller-owned idempotency identity (#3375 axis D). Absent => legacy
    // behavior: a fresh random redeem_request_id and no durable ledger row.
    // The ledger throws TypeError on a malformed id, so the format check has to
    // happen here rather than at the call site, or it surfaces as a 500.
    const hasOperationId = body.operationId !== undefined;
    if (hasOperationId && !isCodexResetCreditOperationId(body.operationId)) {
      return jsonResponse({ error: "Invalid operationId format" }, 400);
    }
    const requestedOperationId = hasOperationId ? body.operationId as string : undefined;
    try {
      return await consumeResetCredits(config, accountId, requestedOperationId);
    } catch (e) {
      if (e instanceof PoolQuotaProbeBusyError) {
        const response = jsonResponse({ error: "server_busy", code: "server_busy" }, 503);
        response.headers.set("Retry-After", "1");
        return response;
      }
      return jsonResponse({ error: e instanceof Error ? e.message : "Reset credit consume failed" }, 500);
    }
  }

  if (url.pathname === "/api/codex-auth/login" && req.method === "POST") {
    return handleCodexAuthLoginStart(req, config, convergeCodexCatalog);
  }

  if (url.pathname === "/api/codex-auth/login/code" && req.method === "POST") {
    return handleCodexAuthLoginCode(req);
  }

  if (url.pathname === "/api/codex-auth/login/cancel" && req.method === "POST") {
    return handleCodexAuthLoginCancel(req);
  }

  if (url.pathname === "/api/codex-auth/login-status" && req.method === "GET") {
    return handleCodexAuthLoginStatus(req, url, config);
  }

  return null;
}
