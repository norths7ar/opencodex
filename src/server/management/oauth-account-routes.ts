import { hasHumanDashboardSession } from "./consent-session";
import { isAnthropicOAuthInstance, configuredAnthropicInstance, type AnthropicInstanceId } from "../../providers/anthropic-instance";



import { effectiveAnthropicAccountThresholdForInstance } from "../../oauth/anthropic-account-threshold";

import { randomBytes, randomUUID } from "node:crypto";



import { hasOwnProvider, isValidProviderName, readConfigDiagnostics, reconcileLiveConfigFromDisk, saveConfigPreservingClaudeCode } from "../../config";
import {
  clearLoginState,
  getLoginStatus,
  isCodeLoginFlow,
  isPublicOAuthProvider,
  listOAuthProviders,
  loginFlowAccountId,
  OAUTH_PROVIDERS,
  publicOAuthAuthenticationErrorMessage,
  startLoginFlow,
  submitLoginCode,
  submitManualLoginCode,
  supportsCodeLoginMode,
  waitForLoginSettled,
} from "../../oauth";
import { OAuthMutationBusyError, removeCredential } from "../../oauth/store";
import { cancelKiroDeviceLogin, kiroDeviceConfigBaseline, selectKiroDeviceProfile, startKiroDeviceLogin, statusKiroDeviceLogin, type KiroDeviceMethod } from "../../oauth/kiro-device-login";
import { parseKiroIamOptions } from "../../oauth/kiro-iam-identity-center";

import { emailMaskingEnabled, maskedAccountLabel } from "../../lib/privacy";
import { reconcileLiveStateStores } from "../../lib/state-store-registrations";
import { listKeyLoginProviders } from "../../oauth/key-providers";



import { clearAccountQuotaCache, clearProviderQuotaCache, clearProviderQuotaCacheFor, fetchProviderAccountQuotas, fetchProviderApiKeyQuotas, providerOAuthAccountQuotaMode, providerApiKeyQuotaMode, readPassiveProviderAccountQuotas } from "../../providers/quota";















import type { OcxApiKeyEntry, OcxConfig } from "../../types";




import { AUTH_MATRIX, jsonResponse } from "../auth-cors";

import { buildApiAccessEndpoints } from "./api-access";
import {
  abortApiKeyRotation,
  commitApiKeyRotation,
  removeExpiredApiKeyRotations,
  startApiKeyRotation,
} from "./api-key-rotation";

import { isPlainRecord } from "./shared";

import type { ManagementContext } from "./context";
import { readManagementJsonBody, readManagementJsonBodyOr, rethrowManagementBodyTooLarge } from "./body";
import { codexAccountNamespaceProviderCollisionError } from "../../codex/account-namespace-match";

/**
 * Provider ids that share the Devin cloud-direct client, and therefore share its
 * process-memory caches.
 *
 * `devin-cli` is a deprecated alias for the merged `devin` provider, but a
 * config row the startup migration has not rekeyed yet can still arrive here —
 * and its logout/removal must clear the same caches, because both ids hand the
 * same api_key to the same client and one cache serves both.
 */
function isDevinCloudDirectProvider(provider: string): boolean {
  return provider === "devin" || provider === "devin-cli";
}

/**
 * Quota invalidation after an account mutation. A B mutation retires only B's provider-level
 * report, routing row and per-account rows; A's cache, epochs and in-flight probes survive.
 * Other providers keep the global clear; `accountRows` also drops that provider's account rows.
 */
function clearQuotaCachesAfterAccountMutation(provider: string, accountRows: boolean): void {
  if (provider === "anthropic2") {
    clearProviderQuotaCacheFor(provider);
    clearAccountQuotaCache(provider);
    return;
  }
  clearProviderQuotaCache();
  if (accountRows) clearAccountQuotaCache(provider);
}

async function clearDevinCloudDirectCaches(): Promise<void> {
  const { clearCachedUserJwt, clearCachedCatalog } = await import("../../adapters/devin/cloud-direct");
  clearCachedUserJwt();
  clearCachedCatalog();
}
/**
 * Parses a bounded JSON object body, or null. Malformed JSON is swallowed; an
 * oversized body still throws so the management dispatcher can return 413.
 * a malformed body, which used to surface as a 500 from the key routes.
 */
async function readJsonBody(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const parsed: unknown = await readManagementJsonBody(req);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch (error) {
    rethrowManagementBodyTooLarge(error);
    return null;
  }
}

/**
 * The single place key-name rules live. The config read schema is deliberately
 * permissive so an existing config can never become unloadable; this is the write
 * boundary that keeps new junk out. A non-string name used to reach `.trim()` and
 * throw.
 */
function validateKeyName(
  raw: unknown,
  opts: { required: boolean },
): { value: string } | { error: string } {
  if (raw === undefined || raw === null) {
    return opts.required ? { error: "name required" } : { value: "" };
  }
  if (typeof raw !== "string") return { error: "name must be a string" };
  // Check the RAW string: trimming first would silently accept "deploy\n" by
  // deleting the very character being rejected.
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(raw)) return { error: "invalid name" };
  const value = raw.trim();
  if (opts.required && !value) return { error: "name required" };
  if (value.length > 64) return { error: "name too long" };
  return { value };
}

export type IssuedApiKey = Pick<OcxApiKeyEntry, "id" | "name" | "key" | "createdAt">;

/** Shared one-time data-key issuance used by the dashboard and link transactions. */
export function issueApiKeyInProcess(config: OcxConfig, name: string): IssuedApiKey {
  const checked = validateKeyName(name, { required: true });
  if ("error" in checked) throw new Error(checked.error);
  const entry: IssuedApiKey = {
    id: randomUUID(),
    name: checked.value,
    key: `ocx_data_${randomBytes(20).toString("hex")}`,
    createdAt: new Date().toISOString(),
  };
  const previous = config.apiKeys;
  config.apiKeys = [...(previous ?? []), entry];
  try {
    saveConfigPreservingClaudeCode(config);
    reconcileLiveStateStores();
  } catch (error) {
    config.apiKeys = previous;
    throw error;
  }
  return entry;
}

/** Revoke and persist one data key; callers can retain their own record on false. */
export function revokeApiKeyInProcess(config: OcxConfig, id: string): boolean {
  const before = config.apiKeys ?? [];
  if (!before.some(key => key.id === id)) return false;
  config.apiKeys = before.filter(key => key.id !== id);
  try {
    saveConfigPreservingClaudeCode(config);
    reconcileLiveStateStores();
  } catch (error) {
    config.apiKeys = before;
    throw error;
  }
  return true;
}

function canStartManagementOAuth(provider: string, ctx: ManagementContext): boolean {
  return provider !== "meta-muse" || hasHumanDashboardSession(ctx);
}

function metaMuseConsentRequired(provider: string, ctx: ManagementContext): Response | null {
  if (canStartManagementOAuth(provider, ctx)) return null;
  return jsonResponse({
    error: "Meta Muse login requires acknowledgement in the OpenCodex dashboard.",
    code: "oauth_consent_required",
  }, 403);
}

function genericOAuthProviderConfig(provider: string, config: ManagementContext["config"]) {
  const configured = config.providers[provider];
  if (provider === "anthropic2" && !configuredAnthropicInstance(config, provider)) return undefined;
  if (configured) return configured;
  const definition = OAUTH_PROVIDERS[provider];
  return definition?.resolveProviderConfig?.(config) ?? definition?.providerConfig;
}

export async function handleOauthAccountRoutes(ctx: ManagementContext): Promise<Response | null> {
  const { req, url, config, deps, principal, syncClaudeAgentDefsBestEffort } = ctx;

  if (["/api/pool/settings", "/api/oauth/accounts/pool", "/api/oauth/accounts/affinity-reset",
    "/api/oauth/accounts/import", "/api/oauth/accounts/auto-switch", "/api/oauth/accounts/active", "/api/providers/keys/active"].includes(url.pathname)) {
    return jsonResponse({ error: "Account pools are not supported; configure a separate channel instead" }, 410);
  }

  if (url.pathname === "/api/accounts/events" && req.method === "GET") {
    const { accountSelectionStream } = await import("./account-selection-stream");
    return accountSelectionStream(req, () => ctx.sessionControl?.isCurrent(req, config) === true);
  }

  // Which providers support real OAuth login (drives the GUI's "Log in with …" buttons).
  if (url.pathname === "/api/oauth/providers" && req.method === "GET") {
    // Discovery reflects this principal's admission; hiding a button is not the
    // consent boundary, which remains independently enforced on both POST routes.
    return jsonResponse({ providers: listOAuthProviders().filter(provider => canStartManagementOAuth(provider, ctx)) });
  }

  // API-key "login" providers (open dashboard → paste key). Drives the GUI's key-provider picker.
  if (url.pathname === "/api/key-providers" && req.method === "GET") {
    return jsonResponse({ providers: listKeyLoginProviders() });
  }

  // OAuth login (xai now; anthropic/kimi in cycle 2). Starts the flow and returns the auth URL;
  // the provider's loopback callback server (inside this process) captures the redirect in the
  // background, then the credential is persisted. The GUI opens the URL and polls /api/oauth/status.
  if (url.pathname === "/api/oauth/login" && req.method === "POST") {
    const body = await readManagementJsonBodyOr(req, {}) as { provider?: string; addAccount?: boolean; accountId?: string; reauth?: boolean; openBrowser?: unknown; method?: unknown; mode?: unknown; startUrl?: unknown; region?: unknown; flowId?: unknown; profileArn?: unknown };
    const provider = (body.provider ?? "").trim().toLowerCase();
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    // Omitted mode is the localhost-callback flow this route has always run.
    if (body.mode !== undefined && body.mode !== "callback" && body.mode !== "code") {
      return jsonResponse({ error: "mode must be \"callback\" or \"code\"" }, 400);
    }
    const codeMode = body.mode === "code";
    if (codeMode && !supportsCodeLoginMode(provider)) {
      return jsonResponse({ error: "code_mode_unsupported" }, 400);
    }
    // Muse may import a local Keychain credential or start a device grant; add-account
    // and reauth skip the import. All management login paths require the dashboard
    // principal before credential acquisition. A raw token proves administration,
    // not acknowledgement; caller-supplied headers are not consent evidence.
    const consentRequired = metaMuseConsentRequired(provider, ctx);
    if (consentRequired) return consentRequired;
    const namespaceCollision = codexAccountNamespaceProviderCollisionError(config.codexAccountNamespaces, provider);
    if (namespaceCollision) return jsonResponse({ error: namespaceCollision }, 409);
    const accountId = body.accountId?.trim();
    const reauth = body.reauth === true || Boolean(accountId);
    if (provider === "kiro") {
      if (reauth && !accountId) return jsonResponse({ error: "Kiro reauth requires an accountId" }, 400);
      if (body.flowId !== undefined) {
        if (reauth || body.method !== undefined || typeof body.flowId !== "string" || body.flowId.length > 128
          || typeof body.profileArn !== "string" || body.profileArn.length > 256) return jsonResponse({ error: "invalid Kiro profile selection" }, 400);
        const owner = principal ?? "admin-token";
        const baseline = kiroDeviceConfigBaseline(body.flowId, owner);
        const status = await selectKiroDeviceProfile(body.flowId, owner, body.profileArn);
        if (!status) return jsonResponse({ error: "unknown Kiro profile selection" }, 404);
        if (status.state === "done") {
          reconcileLiveConfigFromDisk(config, baseline ?? structuredClone(config));
          reconcileLiveStateStores();
        }
        return jsonResponse(status);
      }
      if (body.method !== undefined) {
        if (body.method !== "builder-id" && body.method !== "google" && body.method !== "github" && body.method !== "iam-identity-center") {
          return jsonResponse({ error: "invalid Kiro device method" }, 400);
        }
        if (reauth) return jsonResponse({ error: "native_login_is_add_only" }, 400);
        const iam = body.method === "iam-identity-center" ? parseKiroIamOptions(body) : undefined;
        if (body.method === "iam-identity-center" && !iam) return jsonResponse({ error: "invalid Kiro IAM portal or region" }, 400);
        try {
          return jsonResponse(await startKiroDeviceLogin(body.method as KiroDeviceMethod, principal ?? "admin-token", readConfigDiagnostics().config, iam));
        } catch {
          return jsonResponse({ error: "Kiro device login could not start" }, 409);
        }
      }
    }
    try {
      if (accountId) {
        const { getAccountSet } = await import("../../oauth/store");
        const set = getAccountSet(provider);
        if (!set?.accounts.some(a => a.id === accountId)) {
          return jsonResponse({ error: "Unknown account for reauth" }, 404);
        }
      }
      // Use persisted state, not the live object, as the merge base: another management
      // request may already have mutated live config and yielded before its save.
      const persistedBaseline = readConfigDiagnostics().config;
      // addAccount / reauth forces a fresh browser identity (skips local-CLI token import).
      // Only code mode gets a flow id: it is what a later paste is pinned to. The callback
      // flow's lifecycle argument stays exactly as it was.
      const flowId = codeMode ? randomUUID() : undefined;
      const { url: authUrl, instructions, deviceCode, expiresAt } = await startLoginFlow(provider, {
        forceLogin: body.addAccount === true || reauth,
        ...(accountId ? { reauthAccountId: accountId } : {}),
        ...(codeMode ? { codeMode: true } : {}),
      }, {
        ...(flowId ? { flowId } : {}),
        // startLoginFlow returns the authorization URL before background persistence completes.
        // Three-way reconcile settled disk changes so a failed login cannot leave a provider
        // live-only and an in-flight management mutation cannot be erased before it saves.
        onSettled: () => {
          reconcileLiveConfigFromDisk(config, persistedBaseline);
          reconcileLiveStateStores();
        },
      });
      // Open the browser server-side (the proxy runs on the user's machine) — the GUI's
      // window.open is popup-blocked because it runs after an await, not a direct click.
      //
      // The operator can decline, which is the only way to finish a login in a
      // browser profile other than the OS default, or on a different machine
      // than the proxy. Declining changes nothing else: the URL is still
      // returned below and every login surface renders it with a copy button.
      //
      // The launch outcome is returned rather than discarded, the same contract the Codex
      // account login already keeps: a login whose browser never opened otherwise reads as one
      // that did, and the dashboard can only say so if it is told. `openUrl` answers within its
      // short settle window and never rejects.
      const { shouldOpenBrowserForLogin } = await import("../../oauth/open-browser-choice");
      let browserLaunch: "started" | "failed" | "skipped" = "skipped";
      if (authUrl && !deviceCode && shouldOpenBrowserForLogin(body.openBrowser, config)) {
        const { openUrl } = await import("../../lib/open-url");
        browserLaunch = (await openUrl(authUrl)).status === "started" ? "started" : "failed";
      }
      return jsonResponse({
        url: authUrl,
        instructions,
        deviceCode,
        browserLaunch,
        ...(codeMode ? { mode: "code", flowId, expiresAt } : {}),
      });
    } catch (err) {
      if (err instanceof OAuthMutationBusyError) throw err;
      const message = err instanceof Error ? err.message : String(err);
      const duplicateLoginMessage = `A login for ${provider} is already in progress`;
      return jsonResponse({
        error: message === duplicateLoginMessage
          ? duplicateLoginMessage
          : publicOAuthAuthenticationErrorMessage(err),
      }, 409);
    }
  }

  // Cancel an in-progress browser/device OAuth login (GUI "Cancel" / modal close). Guarded by
  // the same public predicate as /api/oauth/login — only publicly startable flows are cancellable.
  if (url.pathname === "/api/oauth/login/cancel" && req.method === "POST") {
    const body = await readManagementJsonBodyOr(req, {}) as { provider?: string; flowId?: unknown };
    const provider = (body.provider ?? "").trim().toLowerCase();
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    if (provider === "kiro" && body.flowId !== undefined) {
      if (typeof body.flowId !== "string") return jsonResponse({ error: "unknown login flow" }, 404);
      const result = cancelKiroDeviceLogin(body.flowId, principal ?? "admin-token");
      return result ? jsonResponse(result) : jsonResponse({ error: "unknown login flow" }, 404);
    }
    const { cancelLoginFlow } = await import("../../oauth");
    // Without a flow id this cancels the provider's newest in-flight login, which is what the
    // single-user GUI has always meant by "Cancel". A host running concurrent code logins for
    // several users passes the flowId it was given at /api/oauth/login.
    const cancelled = cancelLoginFlow(provider, typeof body.flowId === "string" ? body.flowId : undefined,
      { dismissSettledFailure: true });
    return jsonResponse({ ok: true, cancelled });
  }

  // Manual fallback for browser OAuth: paste the final redirect URL (or authorization code)
  // when the browser cannot reach the loopback callback (remote/SSH/blocked localhost).
  //
  // A code-display flow (started with mode:"code", or identified by its flowId) gets the
  // richer contract instead: the paste is answered with the OUTCOME of the token exchange,
  // not merely with "accepted". A hosted caller has one paste box and no second channel, so
  // "accepted" is indistinguishable from "worked" to the person typing.
  if (url.pathname === "/api/oauth/login/code" && req.method === "POST") {
    const body = await readManagementJsonBodyOr(req, {}) as { provider?: string; input?: string; code?: string; flowId?: unknown };
    const provider = (body.provider ?? "").trim().toLowerCase();
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    const consentRequired = metaMuseConsentRequired(provider, ctx);
    if (consentRequired) return consentRequired;
    const flowId = typeof body.flowId === "string" ? body.flowId : undefined;
    const maskEmails = emailMaskingEnabled(config);
    // Only a code flow of THIS provider gets the outcome-waiting contract; that flow is single-use,
    // so at most one request ever waits on it. A flow id naming anything else (a callback flow,
    // another provider's flow, nothing) has no code login to complete.
    const codeFlow = isCodeLoginFlow(provider, flowId);
    if (flowId !== undefined && !codeFlow) return jsonResponse({ ok: false, error: "no_pending_login" }, 409);
    const input = typeof body.input === "string" ? body.input : typeof body.code === "string" ? body.code : "";
    // Authorization responses are measured in hundreds of bytes; never accept the
    // generic management-body allowance here.
    if (input.length > 4096) {
      return codeFlow
        ? jsonResponse({ ok: false, error: "malformed_input" }, 400)
        : jsonResponse({ error: "input too long" }, 400);
    }
    if (!codeFlow) {
      const result = submitManualLoginCode(provider, input);
      if (!result.ok) return jsonResponse({ error: result.error }, 409);
      return jsonResponse({ ok: true });
    }
    const submitted = submitLoginCode(provider, input, flowId);
    // Rejected before any token request: the flow stays alive so the user can paste again.
    if (!submitted.ok) {
      return jsonResponse({ ok: false, error: submitted.code }, submitted.code === "no_pending_login" ? 409 : 400);
    }
    // The exchange itself times out at 30s inside the provider client; this only has to
    // outlast it so a hung socket surfaces as an answer rather than as a hung request.
    // Pinned to the flow this paste actually landed in: with several code logins in flight for
    // one provider, "the newest flow" by the time this resolves may be somebody else's.
    const settled = await waitForLoginSettled(provider, 60_000, submitted.flowId);
    const status = getLoginStatus(provider, maskEmails, submitted.flowId);
    if (!settled) return jsonResponse({ ok: false, error: "provider_unreachable" }, 400);
    if (status.error) {
      const failure = status.errorCode ?? "invalid_or_expired_code";
      return jsonResponse({ ok: false, error: failure }, failure === "no_pending_login" ? 409 : 400);
    }
    // A flow dropped while waiting (logout, clear) never completed, whatever the store now holds.
    if (status.status !== "complete") return jsonResponse({ ok: false, error: "no_pending_login" }, 409);
    // The account THIS flow wrote, which need not be the active one (reauth of an inactive account,
    // or a concurrent login that finished later).
    const savedId = loginFlowAccountId(provider, submitted.flowId);
    const account = savedId === undefined ? undefined : status.accounts?.find(entry => entry.id === savedId);
    return jsonResponse({ ok: true, ...(account ? { account: { id: account.id, email: account.email } } : {}) });
  }

  if (url.pathname === "/api/oauth/status" && req.method === "GET") {
    const provider = (url.searchParams.get("provider") ?? "").trim().toLowerCase();
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    if (provider === "kiro" && url.searchParams.has("flowId")) {
      const flowId = url.searchParams.get("flowId") ?? "";
      const baseline = kiroDeviceConfigBaseline(flowId, principal ?? "admin-token");
      const status = await statusKiroDeviceLogin(flowId, principal ?? "admin-token");
      if (!status) return jsonResponse({ error: "unknown login flow" }, 404);
      if (status.state === "done") {
        reconcileLiveConfigFromDisk(config, baseline ?? structuredClone(config));
        reconcileLiveStateStores();
      }
      return jsonResponse(status);
    }
    // Resolved here, at the request boundary that already holds the config, and passed down.
    // getLoginStatus stays free of config I/O. This route does not re-mask afterwards: it
    // consumes the already-projected status rather than redacting a second time.
    const status = getLoginStatus(provider, emailMaskingEnabled(config));
    return jsonResponse(status);
  }

  if (url.pathname === "/api/oauth/logout" && req.method === "POST") {
    const provider = (url.searchParams.get("provider") ?? "").trim().toLowerCase();
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    await removeCredential(provider);
    reconcileLiveStateStores();
    clearLoginState(provider);
    const { clearModelCache } = await import("../../codex/model-cache");
    const { clearGatherRoutedModelsInflight } = await import("../../codex/catalog");
    clearModelCache(provider);
    clearGatherRoutedModelsInflight();
    // Drop cached/last-good quota rows tied to the removed credential.
    clearQuotaCachesAfterAccountMutation(provider, true);
    // The cached user_jwt's payload contains the api_key, and the catalog is
    // keyed by that key. Without this they outlive the credential in process
    // memory until the JWT's own ~24 minute expiry. `devin-cli` is a deprecated
    // alias whose unmigrated rows share the one cache, so gating on `devin`
    // alone left a CLI-imported key's JWT resident after its own logout.
    if (isDevinCloudDirectProvider(provider)) await clearDevinCloudDirectCaches();
    return jsonResponse({ success: true });
  }

  // Multiauth account management: list a provider's logged-in accounts, switch the active
  // one, or remove one. Emails are masked; tokens never leave the store.
  if (url.pathname === "/api/oauth/accounts" && req.method === "GET") {
    const provider = (url.searchParams.get("provider") ?? "").trim().toLowerCase();
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    const quotaMode = providerOAuthAccountQuotaMode(provider);
    const quotaProvider = config.providers[provider];
    const { getAccountSet } = await import("../../oauth/store");
    const { isGenericFailoverProvider, kiroAutoSelection } = await import("../../oauth/generic-account-failover");
    const effectiveProvider = genericOAuthProviderConfig(provider, config);
    const supportsPause = effectiveProvider !== undefined
      && (isAnthropicOAuthInstance(provider) && effectiveProvider.authMode === "oauth"
        || isGenericFailoverProvider(provider, effectiveProvider));
    const {
      oauthAccountHealthFields,
      projectOAuthAccountHealth,
      projectStoredOAuthAccountHealth,
    } = await import("../../oauth/health");
    const projectAccounts = () => {
      const set = getAccountSet(provider);
      const current = getLoginStatus(provider, emailMaskingEnabled(config));
      return {
        ...(provider === "anthropic2" ? { provider } : {}), activeAccountId: current.activeAccountId ?? null,
        accounts: (current.accounts ?? []).map(summary => {
          const full = set?.accounts.find(account => account.id === summary.id);
          const health = full
            ? projectStoredOAuthAccountHealth(provider, full)
            : projectOAuthAccountHealth({
              needsReauth: summary.needsReauth === true,
              reauthReason: summary.needsReauth === true ? "refresh_failed" : undefined,
            });
          const maskedLabel = maskedAccountLabel(full?.credential.email);
          return { ...summary, ...(maskedLabel ? { maskedLabel } : {}), ...oauthAccountHealthFields(provider, summary.id, health), quotaMode,
            ...(supportsPause ? { paused: full?.paused === true, ...(full?.pauseDetails ? { pauseDetails: full.pauseDetails } : {}) } : {}),
            ...(isAnthropicOAuthInstance(provider) && supportsPause ? { autoSwitchThresholdOverride: full?.autoSwitchThresholdOverride ?? null,
              effectiveAutoSwitchThreshold: effectiveAnthropicAccountThresholdForInstance(provider as AnthropicInstanceId, config, full),
              autoSwitchThreshold: effectiveAnthropicAccountThresholdForInstance(provider as AnthropicInstanceId, config) } : {}),
            ...(provider === "kiro" && full ? kiroAutoSelection(full) : {}) };
        }),
      };
    };
    // Per-account rate limits: Anthropic reports usage per credential, so every logged-in
    // account can show its own 5h/weekly bars (not just the active one). Opt-in via ?quota=1
    // so the plain account list stays a cheap local read; ?refresh=1 bypasses the TTL.
    const wantQuota = url.searchParams.get("quota") === "1" && quotaMode === "probe";
    // Meta publishes no quota endpoint: its usage is observed in-band on streaming turns
    // and read back from the cache here. `?refresh=1` is accepted and ignored on this
    // path rather than rejected -- the GUI sends it for every provider on a manual
    // refresh, and a 400 would report an error for what is simply a no-op.
    const passiveQuota = url.searchParams.get("quota") === "1" && quotaMode === "passive";
    if (!wantQuota && !passiveQuota) return jsonResponse(projectAccounts());
    const forceRefresh = url.searchParams.get("refresh") === "1";
    // Probing may refresh the active credential and mark needsReauth — project health
    // from the post-probe store so the response is not stale.
    const rows = passiveQuota
      ? readPassiveProviderAccountQuotas(provider)
      : await fetchProviderAccountQuotas(provider, forceRefresh, quotaProvider, config);
    const byId = new Map(rows.map(row => [row.accountId, row]));
    const projected = projectAccounts();
    return jsonResponse({
      ...(provider === "anthropic2" ? { provider } : {}), activeAccountId: projected.activeAccountId,
      accounts: projected.accounts.map(account => {
        const row = byId.get(account.id);
        if (!row) return account;
        if (config.providers[provider] !== quotaProvider || row.isCurrent?.() === false) {
          return { ...account, quota: null, quotaUnavailable: true };
        }
        return {
          ...account,
          quota: row.quota,
          ...(quotaMode === "probe" ? { quotaUnavailable: row.unavailable === true,
            ...(row.unavailable && row.quotaFailure && row.quotaFailureIsCurrent?.() === true ? { quotaFailure: row.quotaFailure } : {}),
          } : {}),
        };
      }),
    });
  }
  if (url.pathname === "/api/oauth/accounts/pause" && req.method === "PUT") {
    const body = await readManagementJsonBodyOr(req, {});
    if (!isPlainRecord(body)) return jsonResponse({ error: "body must be an object" }, 400);
    const provider = typeof body.provider === "string" ? body.provider.trim().toLowerCase() : "";
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    if (typeof body.accountId !== "string" || body.accountId.length === 0) {
      return jsonResponse({ error: "missing accountId" }, 400);
    }
    if (typeof body.paused !== "boolean") return jsonResponse({ error: "paused must be a boolean" }, 400);
    if (body.retryAffinityReset !== undefined && typeof body.retryAffinityReset !== "boolean") {
      return jsonResponse({ error: "retryAffinityReset must be a boolean" }, 400);
    }

    const { isGenericFailoverProvider } = await import("../../oauth/generic-account-failover");
    const effectiveProvider = genericOAuthProviderConfig(provider, config);
    if (!effectiveProvider || !(isAnthropicOAuthInstance(provider) && effectiveProvider.authMode === "oauth"
      || isGenericFailoverProvider(provider, effectiveProvider))) {
      return jsonResponse({ error: "account pause is not supported for this OAuth provider" }, 400);
    }

    const { setAccountPaused } = await import("../../oauth/store");
    const result = await setAccountPaused(provider, body.accountId, body.paused);
    if (result.status === "not-found") return jsonResponse({ error: "account not found" }, 404);

    const anthropicRouting = isAnthropicOAuthInstance(provider)
      ? (await import("../../oauth/anthropic-routing")).anthropicRoutingFor(provider) : undefined;
    let affinityReset: Promise<boolean> | undefined;
    // Explicit retries rebuild reset intent after a server restart lost the in-memory marker.
    if (anthropicRouting && result.activeAccountChanged) {
      anthropicRouting.resetAnthropicRoutingForManualSelection(result.activeAccountId);
      affinityReset = anthropicRouting.flushAnthropicManualSelectionReset();
    } else if (anthropicRouting && body.retryAffinityReset === true) {
      affinityReset = anthropicRouting.resetAnthropicSessionAffinityConfirmed();
    }
    if (result.activeAccountChanged) {
      if (!anthropicRouting) {
        const { genericPoolKey, seedPoolRotationAccount } = await import("../../oauth/pool-kernel");
        seedPoolRotationAccount(genericPoolKey(provider), result.activeAccountId);
      }
      const { clearModelCache } = await import("../../codex/model-cache");
      const { clearGatherRoutedModelsInflight } = await import("../../codex/catalog");
      clearModelCache(provider);
      clearGatherRoutedModelsInflight();
      clearQuotaCachesAfterAccountMutation(provider, false);
    }

    // Inactive pauses also publish invalidation before success; an unchanged retry drains it.
    affinityReset ??= anthropicRouting?.flushAnthropicAccountPause();
    if (affinityReset && !await affinityReset) {
      return jsonResponse({ error: "Account pause was saved, but its session-affinity reset was not persisted. Retry with the same pause state and retryAffinityReset: true.",
        code: "affinity_reset_not_persisted", provider, accountId: body.accountId, paused: body.paused,
        activeAccountId: result.activeAccountId, activeAccountChanged: result.activeAccountChanged,
        pauseCommitted: true, retryable: true }, 503);
    }
    return jsonResponse({
      ok: true,
      provider,
      accountId: body.accountId,
      paused: body.paused,
      activeAccountId: result.activeAccountId,
      activeAccountChanged: result.activeAccountChanged,
    });
  }
  if (url.pathname === "/api/oauth/accounts/clear-cooldown" && req.method === "POST") {
    const body = await readManagementJsonBodyOr(req, {}) as { provider?: unknown; accountId?: unknown };
    const provider = typeof body.provider === "string" ? body.provider.trim().toLowerCase() : "";
    const accountId = typeof body.accountId === "string" ? body.accountId.trim() : "";
    if (!isAnthropicOAuthInstance(provider)) return jsonResponse({ error: "clear-cooldown is only supported for anthropic" }, 400);
    if (provider === "anthropic2" && !configuredAnthropicInstance(config, provider)) return jsonResponse({ error: "Anthropic pool instance is not configured" }, 409);
    if (!accountId) return jsonResponse({ error: "missing accountId" }, 400);
    const { anthropicRoutingFor } = await import("../../oauth/anthropic-routing");
    const cleared = anthropicRoutingFor(provider).clearAnthropicAccountCooldown(accountId);
    return jsonResponse({ ok: true, cleared });
  }

  if (url.pathname === "/api/oauth/accounts/alias" && req.method === "PUT") {
    const body = await readManagementJsonBodyOr(req, {}) as { provider?: unknown; accountId?: unknown; alias?: unknown };
    const provider = typeof body.provider === "string" ? body.provider.trim().toLowerCase() : "";
    const accountId = typeof body.accountId === "string" ? body.accountId.trim() : "";
    const alias = typeof body.alias === "string" ? body.alias.trim() : "";
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    if (!accountId) return jsonResponse({ error: "missing accountId" }, 400);
    if (typeof body.alias !== "string" || alias.length > 80 || /[\x00-\x1f\x7f]/.test(alias)) {
      return jsonResponse({ error: "alias must be at most 80 printable characters" }, 400);
    }
    const { setAccountAlias } = await import("../../oauth/store");
    if (!(await setAccountAlias(provider, accountId, alias || undefined))) return jsonResponse({ error: "account not found" }, 404);
    return jsonResponse({ ok: true, provider, accountId, alias: alias || null });
  }
  if (url.pathname === "/api/oauth/accounts" && req.method === "DELETE") {
    const provider = (url.searchParams.get("provider") ?? "").trim().toLowerCase();
    const id = url.searchParams.get("id") ?? "";
    if (!isPublicOAuthProvider(provider)) return jsonResponse({ error: "unknown oauth provider" }, 400);
    if (!id) return jsonResponse({ error: "missing id" }, 400);
    const retryReset = url.searchParams.get("retryAffinityReset");
    if (retryReset !== null && retryReset !== "true" && retryReset !== "false") {
      return jsonResponse({ error: "retryAffinityReset must be true or false" }, 400);
    }
    if (retryReset === "true" && !isAnthropicOAuthInstance(provider)) {
      return jsonResponse({ error: "session-affinity reset is only supported for Anthropic OAuth instances" }, 400);
    }
    const { removeAccount, getAccountSet } = await import("../../oauth/store");
    if (!(await removeAccount(provider, id))) return jsonResponse({ error: "account not found" }, 404);
    reconcileLiveStateStores();
    let affinityReset: Promise<boolean> | undefined;
    if (isAnthropicOAuthInstance(provider)) {
      const { anthropicRoutingFor } = await import("../../oauth/anthropic-routing");
      const routing = anthropicRoutingFor(provider);
      routing.clearAnthropicAccountCooldown(id);
      routing.clearAnthropicSessionAffinityForAccount(id);
      affinityReset = retryReset === "true" ? routing.resetAnthropicSessionAffinityConfirmed()
        : routing.flushAnthropicManualSelectionReset();
    }
    if (!getAccountSet(provider)) clearLoginState(provider);
    const { clearModelCache } = await import("../../codex/model-cache");
    const { clearGatherRoutedModelsInflight } = await import("../../codex/catalog");
    clearModelCache(provider);
    clearGatherRoutedModelsInflight();
    clearQuotaCachesAfterAccountMutation(provider, true);
    // Same reasoning as logout. Removing the last account for a provider used to
    // leave the JWT and catalog in memory, because only the logout route cleared
    // them.
    if (isDevinCloudDirectProvider(provider)) await clearDevinCloudDirectCaches();
    if (affinityReset && !await affinityReset) {
      return jsonResponse({ code: "affinity_reset_not_persisted", provider, accountId: id,
        deletionCommitted: true, retryable: true }, 503);
    }
    return jsonResponse({ ok: true, ...(retryReset === "true" ? { affinityResetConfirmed: true } : {}) });
  }

  // Multi-key pool for API-key providers (same GUI dropdown as OAuth multiauth): list masked
  // keys, add one (upserts + activates), switch the active key, or remove one. `apiKey` always
  // mirrors the active entry so routing is untouched.
  if (url.pathname === "/api/providers/keys" && req.method === "GET") {
    const name = (url.searchParams.get("name") ?? "").trim();
    if (!name || !isValidProviderName(name) || !hasOwnProvider(config.providers, name)) return jsonResponse({ error: "unknown provider" }, 404);
    const { listProviderApiKeys } = await import("../../providers/api-keys");
    const projectKeys = () => {
      const listed = listProviderApiKeys(config, name);
      const provider = config.providers[name];
      const quotaMode = provider ? providerApiKeyQuotaMode(name, provider) : "unsupported";
      return { ...listed, keys: listed.keys.map(key => ({ ...key, quotaMode })) };
    };
    const initial = projectKeys();
    if (url.searchParams.get("quota") !== "1" || !initial.keys.some(key => key.quotaMode === "probe")) {
      return jsonResponse(initial);
    }
    const rows = await fetchProviderApiKeyQuotas(config, name, url.searchParams.get("refresh") === "1");
    const byId = new Map(rows.map(row => [row.keyId, row]));
    const current = projectKeys();
    return jsonResponse({
      activeId: current.activeId,
      keys: current.keys.map(key => {
        const row = byId.get(key.id);
        if (!row || key.quotaMode !== "probe") return key;
        if (!row.isCurrent()) return { ...key, quota: null, quotaUnavailable: true };
        // Internal identity/epoch checks never enter the JSON DTO.
        return { ...key, quota: row.quota, quotaUnavailable: row.unavailable === true };
      }),
    });
  }
  if (url.pathname === "/api/providers/keys" && req.method === "POST") {
    const body = await readManagementJsonBodyOr(req, {}) as { name?: string; key?: string; label?: string };
    const name = (body.name ?? "").trim();
    if (!name || !isValidProviderName(name) || !hasOwnProvider(config.providers, name)) return jsonResponse({ error: "unknown provider" }, 404);
    if (typeof body.key !== "string" || !body.key.trim()) return jsonResponse({ error: "key is required" }, 400);
    const { addProviderApiKey } = await import("../../providers/api-keys");
    const result = addProviderApiKey(config, name, body.key, body.label);
    if ("error" in result) return jsonResponse({ error: result.error }, 400);
    const { clearModelCache } = await import("../../codex/model-cache");
    clearModelCache(name);
    const { clearProviderQuotaCache } = await import("../../providers/quota");
    clearProviderQuotaCache();
    const { clearKeyCooldowns, forgetApiKeyRotationCursor } = await import("../../providers/key-failover");
    clearKeyCooldowns(name); // manual key management resets 429 cooldown state
    // ...and the rotation cursor with it. A cursor that predates the operator's choice would
    // hand the next proactive pick straight back to whichever key the pool had reached.
    forgetApiKeyRotationCursor(name);
    return jsonResponse({ ok: true, id: result.id }, 201);
  }
  // Opt-in OS keychain storage (#1221): move the active key and pool into the OS credential
  // store (config keeps references), or restore plaintext. Store verifies the keychain before
  // touching config so an unavailable store refuses instead of half-migrating.
  if (url.pathname === "/api/providers/keychain" && req.method === "GET") {
    const name = (url.searchParams.get("name") ?? "").trim();
    if (!name || !isValidProviderName(name) || !hasOwnProvider(config.providers, name)) return jsonResponse({ error: "unknown provider" }, 404);
    const { probeProviderKeychain, providerKeyStoreKind } = await import("../../providers/key-store");
    const probe = probeProviderKeychain();
    return jsonResponse({
      name,
      store: providerKeyStoreKind(config.providers[name]),
      keychainAvailable: probe.available,
      ...(probe.available ? {} : { keychainUnavailableReason: probe.reason }),
    });
  }
  if (url.pathname === "/api/providers/keychain" && req.method === "POST") {
    const body = await readManagementJsonBodyOr(req, {}) as { name?: unknown; action?: unknown };
    const name = typeof body.name === "string" ? body.name.trim() : "";
    if (!name || !isValidProviderName(name) || !hasOwnProvider(config.providers, name)) return jsonResponse({ error: "unknown provider" }, 404);
    if (body.action !== "store" && body.action !== "restore") return jsonResponse({ error: "action must be store or restore" }, 400);
    const { storeProviderKeyInKeychain, restoreProviderKeyFromKeychain, providerKeyStoreKind } = await import("../../providers/key-store");
    const result = body.action === "store"
      ? storeProviderKeyInKeychain(config, name)
      : restoreProviderKeyFromKeychain(config, name);
    if (!result.ok) return jsonResponse({ error: result.error }, result.status);
    const { clearProviderQuotaCache } = await import("../../providers/quota");
    clearProviderQuotaCache();
    return jsonResponse({ ...result, name, store: providerKeyStoreKind(config.providers[name]) });
  }
  if (url.pathname === "/api/providers/keys/pause" && req.method === "PUT") {
    const body = await readManagementJsonBodyOr(req, {});
    if (!isPlainRecord(body)) return jsonResponse({ error: "body must be an object" }, 400);
    if (typeof body.name !== "string" || typeof body.id !== "string" || typeof body.paused !== "boolean") return jsonResponse({ error: "name, id and paused required" }, 400);
    if (!isValidProviderName(body.name) || !hasOwnProvider(config.providers, body.name)) return jsonResponse({ error: "unknown provider" }, 404);
    const { setProviderApiKeyPaused } = await import("../../providers/api-key-pause");
    return setProviderApiKeyPaused(config, body.name, body.id, body.paused)
      ? jsonResponse({ ok: true }) : jsonResponse({ error: "key not found" }, 404);
  }
  if (url.pathname === "/api/providers/keys/alias" && req.method === "PUT") {
    const body = await readManagementJsonBodyOr(req, {}) as { name?: unknown; id?: unknown; alias?: unknown };
    const name = typeof body.name === "string" ? body.name.trim() : "";
    const id = typeof body.id === "string" ? body.id.trim() : "";
    const alias = typeof body.alias === "string" ? body.alias.trim() : "";
    if (!name || !isValidProviderName(name) || !hasOwnProvider(config.providers, name)) return jsonResponse({ error: "unknown provider" }, 404);
    if (!id) return jsonResponse({ error: "missing id" }, 400);
    if (typeof body.alias !== "string" || alias.length > 80 || /[\x00-\x1f\x7f]/.test(alias)) {
      return jsonResponse({ error: "alias must be at most 80 printable characters" }, 400);
    }
    const { setProviderApiKeyLabel } = await import("../../providers/api-keys");
    if (!setProviderApiKeyLabel(config, name, id, alias || undefined)) return jsonResponse({ error: "key not found" }, 404);
    return jsonResponse({ ok: true, name, id, alias: alias || null });
  }
  if (url.pathname === "/api/providers/keys" && req.method === "DELETE") {
    const name = (url.searchParams.get("name") ?? "").trim();
    const id = url.searchParams.get("id") ?? "";
    if (!name || !isValidProviderName(name) || !hasOwnProvider(config.providers, name)) return jsonResponse({ error: "unknown provider" }, 404);
    if (!id) return jsonResponse({ error: "missing id" }, 400);
    const { removeProviderApiKey } = await import("../../providers/api-keys");
    if (!removeProviderApiKey(config, name, id)) return jsonResponse({ error: "key not found" }, 404);
    const { clearModelCache } = await import("../../codex/model-cache");
    clearModelCache(name);
    const { clearProviderQuotaCache } = await import("../../providers/quota");
    clearProviderQuotaCache();
    const { clearKeyCooldowns, forgetApiKeyRotationCursor } = await import("../../providers/key-failover");
    clearKeyCooldowns(name); // manual key management resets 429 cooldown state
    // ...and the rotation cursor with it. A cursor that predates the operator's choice would
    // hand the next proactive pick straight back to whichever key the pool had reached.
    forgetApiKeyRotationCursor(name);
    return jsonResponse({ ok: true });
  }

  // ---------------------------------------------------------------------------
  // API Keys management
  // ---------------------------------------------------------------------------
  if (url.pathname === "/api/keys" && req.method === "GET") {
    if (removeExpiredApiKeyRotations(config)) {
      saveConfigPreservingClaudeCode(config);
      reconcileLiveStateStores();
    }
    const keys = config.apiKeys ?? [];
    const endpoints = buildApiAccessEndpoints(config, {
      requestUrl: req.url,
      requestHost: req.headers.get("host"),
      requestOrigin: req.headers.get("origin"),
    });
    const { readApiKeyUsageRollup } = await import("./api-key-usage");
    const { rollup, attributionSince, historyTruncated, usageIncomplete, usageIncompleteReason } = await readApiKeyUsageRollup(keys.map(k => k.id), config.managementUsageMaxReadBytes);
    return jsonResponse({
      // 8 random hex past the fixed `ocx_data_` literal: enough to tell two keys
      // apart in a list, with 128 bits of the tail still unrevealed. Masking only
      // 8 characters showed `ocx_data...` for every key ever generated.
      keys: keys.map(k => ({
        id: k.id,
        name: k.name,
        prefix: k.key.slice(0, 17) + "...",
        createdAt: k.createdAt,
        // Scope is metadata, not secret: an operator has to be able to read
        // what a key may reach without minting a replacement to find out.
        ...(k.allowedProviders ? { allowedProviders: [...k.allowedProviders] } : {}),
        ...(k.allowedModels ? { allowedModels: [...k.allowedModels] } : {}),
        ...(k.pendingRotation ? { pendingRotation: {
          id: k.pendingRotation.id,
          createdAt: k.pendingRotation.createdAt,
          expiresAt: k.pendingRotation.expiresAt,
        } } : {}),
        usage: rollup.get(k.id) ?? { requests7d: 0, totalRequests: 0 },
      })),
      // Dataset-level and singular: it describes the usage log, not any one key.
      ...(attributionSince ? { attributionSince } : {}),
      ...(historyTruncated ? { historyTruncated: true } : {}),
      ...(usageIncomplete ? { usageIncomplete: true, usageIncompleteReason } : {}),
      authMatrix: AUTH_MATRIX,
      ...endpoints,
    }, 200, req, config);
  }

  if (url.pathname === "/api/keys/reveal" && req.method === "POST") {
    if (principal !== "gui-session") {
      return jsonResponse({ error: "dashboard session required" }, 403, req, config);
    }
    // Local page bootstrap is sufficient for the dashboard, not stored-secret disclosure.
    const authorized = () => ctx.sessionControl?.canRevealDataKeys?.(req, config) === true;
    const denied = () => jsonResponse({ error: "operator-authorized dashboard session required" }, 403, req, config);
    if (!authorized()) return denied();
    const body = await readJsonBody(req);
    // Body reception can outlive the session or its revocation; do not trust cached admission.
    if (!authorized()) return denied();
    if (!body || Object.keys(body).length !== 1 || typeof body.id !== "string" || !body.id) {
      return jsonResponse({ error: "invalid body" }, 400, req, config);
    }
    const entry = config.apiKeys?.find(key => key.id === body.id);
    if (!entry) return jsonResponse({ error: "key not found" }, 404, req, config);
    const response = jsonResponse({ key: entry.key }, 200, req, config);
    response.headers.set("Cache-Control", "no-store");
    return response;
  }

  if (url.pathname === "/api/keys/rotate" && req.method === "POST") {
    const body = await readJsonBody(req);
    if (!body || Object.keys(body).length !== 1 || typeof body.id !== "string" || !body.id) {
      return jsonResponse({ error: "invalid body" }, 400, req, config);
    }
    const result = startApiKeyRotation(config, body.id);
    if ("error" in result) {
      return jsonResponse({ error: result.error === "not-found" ? "key not found" : "rotation already pending" }, result.error === "not-found" ? 404 : 409, req, config);
    }
    saveConfigPreservingClaudeCode(config);
    reconcileLiveStateStores();
    return jsonResponse(result, 201, req, config);
  }

  if (url.pathname === "/api/keys/rotate/commit" && req.method === "POST") {
    const body = await readJsonBody(req);
    if (!body || Object.keys(body).length !== 2 || typeof body.id !== "string" || !body.id
      || typeof body.rotationId !== "string" || !body.rotationId) {
      return jsonResponse({ error: "invalid body" }, 400, req, config);
    }
    const result = commitApiKeyRotation(config, body.id, body.rotationId);
    if ("error" in result) {
      if (result.error === "expired") saveConfigPreservingClaudeCode(config);
      return jsonResponse({ error: result.error === "not-found" ? "key rotation not found" : `rotation ${result.error}` }, result.error === "not-found" ? 404 : 409, req, config);
    }
    saveConfigPreservingClaudeCode(config);
    reconcileLiveStateStores();
    return jsonResponse({ ok: true }, 200, req, config);
  }

  if (url.pathname === "/api/keys/rotate" && req.method === "DELETE") {
    const body = await readJsonBody(req);
    if (!body || Object.keys(body).length !== 2 || typeof body.id !== "string" || !body.id
      || typeof body.rotationId !== "string" || !body.rotationId) {
      return jsonResponse({ error: "invalid body" }, 400, req, config);
    }
    if (!abortApiKeyRotation(config, body.id, body.rotationId)) {
      return jsonResponse({ error: "key rotation not found or mismatched" }, 409, req, config);
    }
    saveConfigPreservingClaudeCode(config);
    reconcileLiveStateStores();
    return jsonResponse({ ok: true }, 200, req, config);
  }

  if (url.pathname === "/api/keys" && req.method === "POST") {
    const body = await readJsonBody(req);
    if (!body) return jsonResponse({ error: "invalid body" }, 400, req, config);
    const nameField = validateKeyName(body.name, { required: false });
    if ("error" in nameField) return jsonResponse({ error: nameField.error }, 400, req, config);
    const name = nameField.value || "default";
    const entry = issueApiKeyInProcess(config, name); // shared helper uses randomBytes(20)
    return jsonResponse({ id: entry.id, name: entry.name, key: entry.key, createdAt: entry.createdAt }, 201, req, config);
  }

  if (url.pathname === "/api/keys" && req.method === "PATCH") {
    const body = await readJsonBody(req);
    if (!body) return jsonResponse({ error: "invalid body" }, 400, req, config);
    if (typeof body.id !== "string" || !body.id) return jsonResponse({ error: "id required" }, 400, req, config);
    const existing = (config.apiKeys ?? []).find(k => k.id === body.id);
    if (!existing) return jsonResponse({ error: "key not found" }, 404, req, config);
    const entry = { ...existing };
    // Rename and scope are independent edits. A scope-only PATCH must not have
    // to restate the name, and a rename must not silently widen a scope, so
    // each field is applied only when the caller actually sent it.
    const renaming = body.name !== undefined;
    const scopingProviders = body.allowedProviders !== undefined;
    const scopingModels = body.allowedModels !== undefined;
    if (!renaming && !scopingProviders && !scopingModels) {
      return jsonResponse({ error: "name, allowedProviders or allowedModels required" }, 400, req, config);
    }
    if (renaming) {
      const nameField = validateKeyName(body.name, { required: true });
      if ("error" in nameField) return jsonResponse({ error: nameField.error }, 400, req, config);
      entry.name = nameField.value;
    }
    for (const [field, sent] of [["allowedProviders", scopingProviders], ["allowedModels", scopingModels]] as const) {
      if (!sent) continue;
      const value = body[field];
      // `null` and `[]` both clear the list back to unrestricted; anything else
      // must be a list of non-empty strings, because a silently ignored malformed
      // scope would read as "allowed everything" to whoever set it.
      if (value === null) { delete entry[field]; continue; }
      if (!Array.isArray(value) || value.some(item => typeof item !== "string" || !item.trim() || item.length > 256)) {
        return jsonResponse({ error: `${field} must be a list of non-empty names` }, 400, req, config);
      }
      const normalized = [...new Set((value as string[]).map(item => item.trim()))];
      if (normalized.length === 0) delete entry[field];
      else entry[field] = normalized;
    }
    // Publish the validated replacement only after every field is accepted.
    config.apiKeys = config.apiKeys!.map(key => key === existing ? entry : key);
    saveConfigPreservingClaudeCode(config);
    reconcileLiveStateStores();
    // Never echo key material from a rename.
    return jsonResponse({
      id: entry.id,
      name: entry.name,
      createdAt: entry.createdAt,
      ...(entry.allowedProviders ? { allowedProviders: [...entry.allowedProviders] } : {}),
      ...(entry.allowedModels ? { allowedModels: [...entry.allowedModels] } : {}),
    }, 200, req, config);
  }

  if (url.pathname === "/api/keys" && req.method === "DELETE") {
    const body = await readJsonBody(req);
    if (!body) return jsonResponse({ error: "invalid body" }, 400, req, config);
    if (typeof body.id !== "string" || !body.id) return jsonResponse({ error: "id required" }, 400, req, config);
    // A stale id must not read as a successful revocation.
    if (!revokeApiKeyInProcess(config, body.id)) return jsonResponse({ error: "key not found" }, 404, req, config);
    return jsonResponse({ success: true }, 200, req, config);
  }
  return null;
}
