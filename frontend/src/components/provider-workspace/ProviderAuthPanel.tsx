import { anthropicSubscriptionLabel } from "../../../../backend/src/providers/anthropic-subscription";
import {
  anthropicInstanceRowShapeMatches,
  isAnthropicInstanceId,
} from "../../../../backend/src/providers/anthropic-instance-id";
/**
 * ProviderAuthPanel — OAuth accounts, API-key pool, and forward-auth
 * embedding for the workspace Settings tab (WP091). Consumes WP040+WP060
 * handlers via props-down; no internal auth machinery.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useT, useI18n } from "../../i18n/shared";
import { useRuntimeCapabilities, useMigrationLabel } from "../../runtime-capabilities";
import { OAuthFlowSelect } from "../oauth-flow-select";
import { formatAccountPauseTooltip } from "../../account-pause-display";
import { IconLock, IconRefresh, IconTrash } from "../../icons";
import type { WorkspaceItem } from "../../provider-workspace/catalog";
import { oauthAccountDisplayLabel, providerAuthSurface } from "../../provider-workspace/auth";
import { displayAccountId } from "../../lib/privacy";
import {
  formatOAuthHealthLabel,
  formatOAuthHealthSummary,
  oauthHealthBadgeClass,
  oauthHealthIsCooldown,
  oauthHealthShowsReauth,
} from "../../oauth-health-display";
import CodexAccountPool from "../CodexAccountPool";
import { LoginHint as LoginHintView } from "../login-url-block";
import { OpenBrowserPrefToggle } from "../open-browser-pref-toggle";
import ProviderAccountQuota from "./ProviderAccountQuota";
import { GrokCouponBadge, GrokResetCouponModal } from "./GrokResetCoupons";
import { AnthropicGrantBadge, AnthropicResetGrantModal } from "./AnthropicResetGrants";
import type { CodexAccountPoolController } from "../../hooks/useCodexAccountPool";
import { useGrokResetCoupons } from "../../hooks/useGrokResetCoupons";
import { useAnthropicResetGrants } from "../../hooks/useAnthropicResetGrants";
import { Switch } from "../../ui";
import { kiroSkipReasonKey } from "../../kiro-device-login-helpers";
import KiroDeviceLoginDialog from "../KiroDeviceLoginDialog";
import type {
  AccountLoadState,
  OAuthAccountRow,
  ApiKeyRow,
  LoginHint,
  ProviderAuthHandlers,
  ProviderUpdatePatch,
  ProviderUpdateResult,
} from "./types";

const EMPTY_OAUTH_ACCOUNTS: OAuthAccountRow[] = [];
const EMPTY_API_KEYS: ApiKeyRow[] = [];

/**
 * One predicate for "this row cannot spend a coupon right now". The read set and
 * the badge must agree: a row fetched here and hidden there is a billing RPC
 * spent on a 401.
 */
function accountShowsReauth(account: OAuthAccountRow): boolean {
  return Boolean(account.needsReauth) || oauthHealthShowsReauth(account.health?.status);
}

function XaiChatOptInControl({
  initialState,
  onUpdateProvider,
}: {
  initialState: NonNullable<WorkspaceItem["xaiResponsesOptInState"]>;
  onUpdateProvider?: (name: string, patch: ProviderUpdatePatch) => Promise<ProviderUpdateResult>;
}) {
  const t = useT();
  const [state, setState] = useState(initialState);
  const [seenInitialState, setSeenInitialState] = useState(initialState);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  if (initialState !== seenInitialState) {
    setSeenInitialState(initialState);
    setState(initialState);
  }
  const mixed = state === "mixed";

  const toggle = async () => {
    if (!onUpdateProvider || saving) return;
    const next = state === false;
    setSaving(true);
    setError("");
    try {
      const result = await onUpdateProvider("xai", { xaiResponsesOptIn: next });
      if (!result.ok) {
        setError(result.error ?? t("prov.updateFail"));
        return;
      }
      setState(result.xaiResponsesOptInState ?? next);
    } catch {
      setError(t("prov.networkError"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="pwi-auth-optin-row">
      <div className="pwi-auth-optin-copy">
        <span className="pwi-auth-optin-label">{t("pws.xaiChatOptIn")}</span>
        <span className="pwi-auth-row-secondary">
          {t("pws.xaiChatOptInDesc")}
          {mixed && <span className="pwi-auth-optin-mixed"> {t("pws.xaiChatOptInMixed")}</span>}
        </span>
        {error && (
          <span className="pwi-auth-optin-error" role="alert">
            {error}
          </span>
        )}
      </div>
      <Switch
        on={state === false}
        mixed={mixed}
        onClick={() => {
          void toggle();
        }}
        disabled={!onUpdateProvider || saving}
        label={t("pws.xaiChatOptIn")}
      />
    </div>
  );
}

export default function ProviderAuthPanel({
  item,
  apiBase,
  oauth,
  accounts = EMPTY_OAUTH_ACCOUNTS,
  keys = EMPTY_API_KEYS,
  accountLoadState = "ready",
  switchingAccountId = null,
  pausingAccountId = null,
  busy = false,
  loginHint,
  authHandlers,
  onCodexActiveNeedsReauthChange,
  codexController,
  onUpdateProvider,
}: {
  item: WorkspaceItem;
  apiBase: string;
  oauth?: { loggedIn: boolean; email?: string; error?: string };
  accounts?: OAuthAccountRow[];
  keys?: ApiKeyRow[];
  accountLoadState?: AccountLoadState;
  switchingAccountId?: string | null;
  pausingAccountId?: string | null;
  busy?: boolean;
  loginHint?: LoginHint | null;
  authHandlers?: ProviderAuthHandlers;
  onCodexActiveNeedsReauthChange?: (needs: boolean) => void;
  onUpdateProvider?: (name: string, patch: ProviderUpdatePatch) => Promise<ProviderUpdateResult>;
  /** Shared Codex account state owned by Providers (WP3). */
  codexController?: CodexAccountPoolController;
}) {
  const t = useT();
  const { locale } = useI18n();
  const [addingKey, setAddingKey] = useState(false);
  const [kiroChooser, setKiroChooser] = useState<{ addAccount: boolean } | null>(null);
  const kiroLoginTriggerRef = useRef<HTMLButtonElement>(null);
  const kiroAddTriggerRef = useRef<HTMLButtonElement>(null);
  const openLogin = (addAccount: boolean) => {
    if (item.name === "kiro") setKiroChooser({ addAccount });
    else void authHandlers?.onLogin(item.name, addAccount);
  };
  const [newKey, setNewKey] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [manualCode, setManualCode] = useState("");
  const [manualCodeBusy, setManualCodeBusy] = useState(false);
  const [manualCodeMsg, setManualCodeMsg] = useState("");
  const [manualCodeOk, setManualCodeOk] = useState(true);
  const connectionIdentity = JSON.stringify([
    apiBase,
    item.name,
    accounts.find((account) => account.active)?.id,
    keys.find((key) => key.active)?.id,
  ]);
  const [quotaRefreshState, setQuotaRefreshState] = useState<{
    identity: string;
    refreshing: boolean;
    result: { ok: boolean; text: string } | null;
  }>({ identity: connectionIdentity, refreshing: false, result: null });
  const refreshingQuota =
    quotaRefreshState.identity === connectionIdentity && quotaRefreshState.refreshing;
  const quotaRefreshResult =
    quotaRefreshState.identity === connectionIdentity ? quotaRefreshState.result : null;
  const quotaRefreshGeneration = useRef(0);
  useEffect(() => {
    quotaRefreshGeneration.current += 1;
    return () => {
      quotaRefreshGeneration.current += 1;
    };
  }, [connectionIdentity]);

  const capabilities = useRuntimeCapabilities();
  const migrationLabel = useMigrationLabel();
  const onRefreshQuota = capabilities ? undefined : authHandlers?.onRefreshQuota;
  const surface = providerAuthSurface({ ...item, hasApiKey: item.hasApiKey || keys.length > 0 });
  const isOauth = surface === "oauth-accounts";
  const isKeyAuth = surface === "api-keys";
  // Grok reset coupons live behind a billing RPC rather than the quota payload,
  // so the xAI rows read them once per roster instead of riding the quota probe.
  // The gate names the OAuth surface here rather than relying on the roster
  // loader three files away to leave `accounts` empty for key-auth xAI.
  const grokCouponsEnabled = isOauth && item.name === "xai" && accounts.length > 0;
  const grokAccountIds = useMemo(
    () =>
      grokCouponsEnabled
        ? accounts.filter((account) => !accountShowsReauth(account)).map((account) => account.id)
        : [],
    [grokCouponsEnabled, accounts],
  );
  const grokCoupons = useGrokResetCoupons({
    apiBase,
    accountIds: grokAccountIds,
    enabled: grokCouponsEnabled,
  });
  const [couponAccount, setCouponAccount] = useState<OAuthAccountRow | null>(null);
  // Claude usage resets ride a separate usage read, like the Grok coupons above.
  const isAnthropicPool =
    isAnthropicInstanceId(item.name) && anthropicInstanceRowShapeMatches(item.name, item);
  const claudeGrantsEnabled = isOauth && isAnthropicPool && accounts.length > 0;
  const claudeAccountIds = useMemo(
    () =>
      claudeGrantsEnabled
        ? accounts.filter((account) => !accountShowsReauth(account)).map((account) => account.id)
        : [],
    [claudeGrantsEnabled, accounts],
  );
  const claudeGrants = useAnthropicResetGrants({
    apiBase,
    provider: isAnthropicInstanceId(item.name) ? item.name : "anthropic",
    accountIds: claudeAccountIds,
    enabled: claudeGrantsEnabled,
  });
  // The open reset dialog belongs to the pool it was opened in. Switching server or pool
  // hides it during render rather than resetting it from an effect.
  const grantScope = `${apiBase}\0${item.name}`;
  const [grantSelection, setGrantSelection] = useState<{
    scope: string;
    account: OAuthAccountRow;
  } | null>(null);
  const grantAccount = grantSelection?.scope === grantScope ? grantSelection.account : null;
  const setGrantAccount = (account: OAuthAccountRow | null) => {
    setGrantSelection(account ? { scope: grantScope, account } : null);
  };
  const refreshQuota = async () => {
    if (!onRefreshQuota || refreshingQuota) return;
    const generation = ++quotaRefreshGeneration.current;
    // Cleared on click so a previous "refreshed" cannot sit under a later failure.
    setQuotaRefreshState({ identity: connectionIdentity, refreshing: true, result: null });
    try {
      const ok = await onRefreshQuota(item.name);
      if (quotaRefreshGeneration.current === generation)
        setQuotaRefreshState({
          identity: connectionIdentity,
          refreshing: false,
          result: { ok, text: t(ok ? "pws.quotaCheckCompleted" : "codexAuth.quotaRefreshFailed") },
        });
    } catch {
      if (quotaRefreshGeneration.current === generation)
        setQuotaRefreshState({
          identity: connectionIdentity,
          refreshing: false,
          result: { ok: false, text: t("codexAuth.quotaRefreshFailed") },
        });
    }
  };

  if (surface === "codex-accounts") {
    return (
      <section className="pwi-section pwi-auth-section" aria-label={t("pws.availableAccounts")}>
        <h3 className="pwi-section-title">{t("pws.availableAccounts")}</h3>
        <div className="pwi-auth-body">
          <CodexAccountPool
            apiBase={apiBase}
            embedded
            controller={codexController}
            onActiveNeedsReauthChange={onCodexActiveNeedsReauthChange}
          />
        </div>
      </section>
    );
  }

  if (!surface || !authHandlers) return null;

  const hintForThis = loginHint?.provider === item.name ? loginHint : null;
  // Paste fallback for when the browser cannot reach the loopback callback
  // (remote dashboard, SSH, blocked localhost). A rejected paste reports why and
  // leaves the flow running, so the user can correct it and try again.
  const submitManualCode = async () => {
    const input = manualCode.trim();
    if (!input || manualCodeBusy) return;
    setManualCodeBusy(true);
    setManualCodeMsg("");
    try {
      const res = await fetch(`${apiBase}/api/oauth/login/code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: item.name, input }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        setManualCodeOk(false);
        setManualCodeMsg(t("prov.pasteFail", { error: data.error || res.statusText }));
        return;
      }
      setManualCode("");
      setManualCodeOk(true);
      setManualCodeMsg(t("prov.pasteOk"));
    } catch {
      setManualCodeOk(false);
      setManualCodeMsg(t("modal.networkError"));
    } finally {
      setManualCodeBusy(false);
    }
  };
  const loggedIn = accounts.length > 0 || oauth?.loggedIn === true;
  const activeReauthAccount = accounts.find((a) => a.active && a.needsReauth);
  const activeNeedsReauth = Boolean(activeReauthAccount);
  const quotaRows = isOauth ? accounts : keys;
  const canRefreshQuota =
    Boolean(onRefreshQuota) &&
    !(quotaRows.length > 0 && quotaRows.every((row) => row.quotaMode === "unsupported"));

  const submitKey = async () => {
    const key = newKey.trim();
    if (!key) return;
    setKeyBusy(true);
    try {
      const ok = await authHandlers.onAddApiKey(item.name, key);
      if (ok) {
        setNewKey("");
        setAddingKey(false);
      }
    } finally {
      setKeyBusy(false);
    }
  };

  return (
    <section
      className="pwi-section pwi-auth-section"
      aria-label={isOauth ? t("pws.availableAccounts") : t("pws.apiKeys")}
    >
      {/*
        The refresh control is in the section HEAD, not only at the foot of the list.
        Every account renders a stack of 5-hour/weekly/Fable bars, so with two accounts
        the footer copy sits well below the fold: an operator looking straight at stale
        bars had to scroll past all of them to find the button that re-reads them. The
        header keeps it beside the numbers it refreshes; the footer copy stays where it
        is, next to "Add account", because that is the account-management cluster.
      */}
      <div className="pwi-auth-head">
        <h3 className="pwi-section-title">
          {isOauth ? t("pws.availableAccounts") : t("pws.apiKeys")}
        </h3>
        {((isOauth && loggedIn) || isKeyAuth) && canRefreshQuota && (
          <div className="pwi-auth-head-actions">
            {quotaRefreshResult && (
              <span
                role="status"
                className={quotaRefreshResult.ok ? "pws-status-ok" : "pws-status-warn"}
              >
                {quotaRefreshResult.text}
              </span>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={refreshingQuota || busy || Boolean(switchingAccountId)}
              onClick={() => {
                void refreshQuota();
              }}
            >
              <IconRefresh width={14} height={14} aria-hidden="true" />{" "}
              {refreshingQuota ? t("codexAuth.refreshingQuota") : t("codexAuth.refreshQuota")}
            </button>
          </div>
        )}
      </div>
      <div className="pwi-auth-body">
        {!capabilities && item.name === "xai" && (
          <XaiChatOptInControl
            initialState={item.xaiResponsesOptInState ?? true}
            onUpdateProvider={onUpdateProvider}
          />
        )}
        {isOauth && (
          <>
            <div className="pwi-auth-status-row">
              <span
                className={`pwi-auth-dot ${activeNeedsReauth ? "pwi-auth-dot--warn" : loggedIn ? "pwi-auth-dot--ok" : "pwi-auth-dot--off"}`}
                aria-hidden="true"
              />
              <span className="pwi-auth-status-text">
                {loggedIn ? t("pws.loggedInTitle") : oauth?.error || t("pws.notLoggedInTitle")}
              </span>
              <span className="pwi-auth-actions">
                {capabilities?.oauthProviders?.includes(item.name) &&
                  loggedIn &&
                  !activeReauthAccount && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      disabled={busy}
                      onClick={() => openLogin(false)}
                    >
                      {locale === "zh" || locale === "zh-TW"
                        ? "重新登录 / 更换账号"
                        : "Sign in again / replace account"}
                    </button>
                  )}
                {activeReauthAccount && (
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={busy}
                    onClick={() => void authHandlers.onReauth(item.name, activeReauthAccount.id)}
                  >
                    {t("pws.reauthenticate")}
                  </button>
                )}
                {loggedIn ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={busy}
                    onClick={() => void authHandlers.onLogout(item.name)}
                  >
                    {t("prov.logout")}
                  </button>
                ) : (
                  <button
                    ref={item.name === "kiro" ? kiroLoginTriggerRef : undefined}
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={busy || (item.name === "kiro" && Boolean(kiroChooser))}
                    onClick={() => openLogin(false)}
                  >
                    {busy ? (
                      <span className="pwi-spin-inline" aria-hidden="true" />
                    ) : (
                      <IconLock style={{ width: 13, height: 13 }} aria-hidden="true" />
                    )}
                    {busy ? t("prov.waitingBrowser") : t("prov.login")}
                  </button>
                )}
              </span>
            </div>
            {!busy && (
              <>
                <OAuthFlowSelect provider={item.name} />
                <OpenBrowserPrefToggle />
              </>
            )}
            {busy && hintForThis && (
              <div className="pwi-auth-wait">
                <span className="pwi-spin-inline" aria-hidden="true" />
                <div className="pwi-auth-wait-copy">
                  <div className="pwi-auth-wait-title">{t("prov.waitingBrowser")}</div>
                  <LoginHintView
                    hint={{
                      url: hintForThis.url,
                      deviceCode: hintForThis.deviceCode,
                      instructions: hintForThis.instructions,
                      browserLaunch: hintForThis.browserLaunch,
                    }}
                    paste={{
                      value: manualCode,
                      busy: manualCodeBusy,
                      message: manualCodeMsg,
                      ok: manualCodeOk,
                      onChange: setManualCode,
                      onSubmit: () => {
                        void submitManualCode();
                      },
                    }}
                  />
                  {authHandlers.onCancelLogin && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => void authHandlers.onCancelLogin?.(item.name)}
                    >
                      {t("common.cancel")}
                    </button>
                  )}
                </div>
              </div>
            )}
            {authHandlers.pendingAffinityResets?.[item.name] && (
              <div className="pwi-auth-state pwi-auth-state--error" role="alert">
                <span>{t("prov.affinityResetPending")}</span>
                {authHandlers.onRetryAffinityReset && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={Boolean(switchingAccountId || pausingAccountId)}
                    onClick={() => void authHandlers.onRetryAffinityReset?.(item.name)}
                  >
                    {t("common.retry")}
                  </button>
                )}
              </div>
            )}
            {accountLoadState === "loading" && accounts.length === 0 && (
              <div className="pwi-auth-state" role="status">
                <span className="pwi-spin-inline" aria-hidden="true" />
                {t("pws.accountsLoading")}
              </div>
            )}
            {accountLoadState === "error" && (
              <div className="pwi-auth-state pwi-auth-state--error" role="alert">
                <span>{t("pws.accountsLoadFailed")}</span>
                {authHandlers.onRetryAccounts && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => void authHandlers.onRetryAccounts?.(item.name)}
                  >
                    {t("pws.retryAccounts")}
                  </button>
                )}
              </div>
            )}
            {accounts.length > 0 && (
              <ul className="pwi-auth-list">
                {accounts.map((account) => {
                  const label = oauthAccountDisplayLabel(accounts, account, t);
                  const planLabel = isAnthropicPool
                    ? anthropicSubscriptionLabel(account.plan)
                    : item.name === "google-antigravity"
                      ? account.plan
                      : undefined;
                  const switching = switchingAccountId === account.id;
                  const pausing = pausingAccountId === account.id;
                  const healthStatus = account.health?.status;
                  const showReauth = accountShowsReauth(account);
                  const inCooldown = oauthHealthIsCooldown(healthStatus);
                  const maskedId = displayAccountId(account.id);
                  const healthLabel = formatOAuthHealthLabel(t, account.health);
                  const healthSummary = isAnthropicPool
                    ? null
                    : formatOAuthHealthSummary(t, item.name, account.id, account.health);
                  return (
                    <li
                      key={account.id}
                      className={`pwi-auth-acct${account.active ? " pwi-auth-acct--active" : ""}`}
                    >
                      <div
                        className={`pwi-auth-row${account.active ? " pwi-auth-row--active" : ""}`}
                      >
                        <button
                          type="button"
                          className="pwi-auth-row-main"
                          onClick={() => {
                            if (
                              !account.active &&
                              !account.paused &&
                              !showReauth &&
                              !inCooldown &&
                              !switchingAccountId &&
                              !pausingAccountId
                            )
                              void authHandlers.onSwitchAccount(item.name, account);
                          }}
                          aria-current={account.active ? "true" : undefined}
                          aria-label={`${label}${planLabel ? ` — ${planLabel}` : ""}${account.active ? ` — ${t("pws.accountCurrent")}` : ""}`}
                          disabled={Boolean(
                            account.paused ||
                            showReauth ||
                            inCooldown ||
                            switchingAccountId ||
                            pausingAccountId,
                          )}
                        >
                          <span
                            className={`pwi-auth-dot ${showReauth ? "pwi-auth-dot--warn" : account.active && !account.paused ? "pwi-auth-dot--ok" : "pwi-auth-dot--off"}`}
                            aria-hidden="true"
                          />
                          <span className="pwi-auth-row-copy">
                            <span className="pwi-auth-row-heading">
                              <span className="pwi-auth-row-label">{label}</span>
                              {planLabel && <span className="badge badge-green">{planLabel}</span>}
                            </span>
                            {!isAnthropicPool && (
                              <span className="pwi-auth-row-secondary">{`${t("prov.accountId")}: ${maskedId}`}</span>
                            )}
                            {healthSummary && (
                              <span className="pwi-auth-row-secondary faint">{healthSummary}</span>
                            )}
                            {inCooldown && (
                              <span className="pwi-auth-row-secondary faint">
                                {t("pws.healthCooldownHint")}
                              </span>
                            )}
                            {account.paused && (
                              <span className="pwi-auth-row-secondary faint">
                                {t("pws.accountPausedHint")}
                              </span>
                            )}
                          </span>
                          {healthLabel && (
                            <span className={oauthHealthBadgeClass(healthStatus)}>
                              {healthLabel}
                            </span>
                          )}
                          {showReauth && !healthLabel && (
                            <span className="badge badge-amber">{t("pws.reauth")}</span>
                          )}
                          {kiroSkipReasonKey(account, item.name) && (
                            <span className="badge badge-amber">
                              {t(kiroSkipReasonKey(account, item.name)!)}
                            </span>
                          )}
                          {account.paused && (
                            <span
                              className="badge badge-muted"
                              title={
                                account.pauseDetails
                                  ? formatAccountPauseTooltip(t, locale, account.pauseDetails)
                                  : undefined
                              }
                            >
                              {t("codexAuth.paused")}
                            </span>
                          )}
                          {account.active && !account.paused && (
                            <span className="badge badge-primary">{t("prov.accountActive")}</span>
                          )}
                          {switching && (
                            <span className="badge badge-muted">{t("pws.accountSwitching")}</span>
                          )}
                        </button>
                        {typeof account.paused === "boolean" && authHandlers.onPauseAccount && (
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            aria-label={`${t(account.paused ? "codexAuth.resume" : "codexAuth.pause")} — ${label}`}
                            title={account.paused ? t("pws.accountPausedHint") : undefined}
                            aria-busy={pausing}
                            disabled={
                              busy || Boolean(switchingAccountId) || Boolean(pausingAccountId)
                            }
                            onClick={() =>
                              void authHandlers.onPauseAccount(item.name, account, !account.paused)
                            }
                          >
                            {t(account.paused ? "codexAuth.resume" : "codexAuth.pause")}
                          </button>
                        )}
                        {showReauth && (
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            disabled={
                              busy || Boolean(switchingAccountId) || Boolean(pausingAccountId)
                            }
                            onClick={() => void authHandlers.onReauth(item.name, account.id)}
                          >
                            {t("pws.reauthenticate")}
                          </button>
                        )}
                        {grokCouponsEnabled && !showReauth && (
                          <GrokCouponBadge
                            entry={grokCoupons.entries[account.id]}
                            t={t}
                            onClick={() => setCouponAccount(account)}
                          />
                        )}
                        {claudeGrantsEnabled && !showReauth && (
                          <AnthropicGrantBadge
                            entry={claudeGrants.entries[account.id]}
                            t={t}
                            onClick={() => setGrantAccount(account)}
                          />
                        )}
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          onClick={() =>
                            void authHandlers.onEditAlias(
                              item.name,
                              "oauth",
                              account.id,
                              account.alias,
                            )
                          }
                        >
                          {t("prov.editAlias")}
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm pwi-auth-row-remove"
                          aria-label={`${t("common.remove")} — ${label}`}
                          title={`${t("common.remove")} — ${label}`}
                          disabled={Boolean(switchingAccountId) || Boolean(pausingAccountId)}
                          onClick={() => void authHandlers.onRemoveAccount(item.name, account)}
                        >
                          <IconTrash style={{ width: 13, height: 13 }} aria-hidden="true" />
                        </button>
                      </div>
                      <div className="pwi-auth-acct-quota">
                        <ProviderAccountQuota
                          quotaMode={account.quotaMode}
                          quota={account.quota}
                          quotaUnavailable={account.quotaUnavailable}
                          quotaPending={account.quotaPending}
                          quotaFailure={account.quotaFailure}
                        />
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
            {couponAccount && (
              <GrokResetCouponModal
                accountId={couponAccount.id}
                accountLabel={oauthAccountDisplayLabel(accounts, couponAccount, t)}
                entry={grokCoupons.entries[couponAccount.id]}
                controller={grokCoupons}
                onClose={() => setCouponAccount(null)}
              />
            )}
            {grantAccount && (
              <AnthropicResetGrantModal
                key={`${apiBase}:${item.name}:${grantAccount.id}`}
                accountId={grantAccount.id}
                accountLabel={oauthAccountDisplayLabel(accounts, grantAccount, t)}
                entry={claudeGrants.entries[grantAccount.id]}
                controller={claudeGrants}
                onClose={() => setGrantAccount(null)}
              />
            )}
            {accountLoadState === "ready" && loggedIn && accounts.length === 0 && (
              <div className="pwi-auth-state pwi-auth-state--empty">{t("pws.noAccounts")}</div>
            )}
            {loggedIn && (
              <div className="pwi-auth-actions">
                {canRefreshQuota && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={refreshingQuota || busy || Boolean(switchingAccountId)}
                    onClick={() => {
                      void refreshQuota();
                    }}
                  >
                    <IconRefresh width={14} height={14} aria-hidden="true" />{" "}
                    {refreshingQuota ? t("codexAuth.refreshingQuota") : t("codexAuth.refreshQuota")}
                  </button>
                )}
                {/*
                  The result line lives in the section head only. Rendering it in both
                  places would announce one refresh twice to a screen reader, since both
                  spans carry role="status".
                */}
              </div>
            )}
          </>
        )}
        {item.name === "kiro" && kiroChooser && (
          <KiroDeviceLoginDialog
            apiBase={apiBase}
            triggerRef={kiroChooser.addAccount ? kiroAddTriggerRef : kiroLoginTriggerRef}
            addAccount={kiroChooser.addAccount}
            busy={busy}
            onClose={() => setKiroChooser(null)}
            onCli={(addAccount) => {
              void authHandlers.onLogin(item.name, addAccount);
            }}
            onSettled={authHandlers.onNativeLoginSettled}
          />
        )}

        {isKeyAuth && (
          <>
            {keys.length > 0 && (
              <ul className="pwi-auth-list">
                {keys.map((entry) => (
                  <li
                    key={entry.id}
                    className={`pwi-auth-acct${entry.active ? " pwi-auth-acct--active" : ""}`}
                  >
                    <div className={`pwi-auth-row${entry.active ? " pwi-auth-row--active" : ""}`}>
                      <button
                        type="button"
                        className="pwi-auth-row-main"
                        onClick={() => void authHandlers.onSwitchApiKey(item.name, entry)}
                        disabled={entry.active && !entry.disabled}
                        aria-label={
                          entry.disabled
                            ? `${t("codexAuth.resume")} — ${entry.label ?? entry.masked}`
                            : undefined
                        }
                      >
                        <span
                          className={`pwi-auth-dot ${entry.active ? "pwi-auth-dot--ok" : "pwi-auth-dot--off"}`}
                          aria-hidden="true"
                        />
                        <span className="pwi-auth-row-copy">
                          <span className="pwi-auth-row-label">{entry.label ?? entry.masked}</span>
                          {entry.label && (
                            <code className="pwi-auth-row-secondary">
                              {entry.masked} · {t("prov.accountId")}: {entry.id}
                            </code>
                          )}
                        </span>
                        {entry.disabled && (
                          <span
                            className="badge badge-muted"
                            title={
                              entry.pauseDetails
                                ? formatAccountPauseTooltip(t, locale, entry.pauseDetails)
                                : undefined
                            }
                          >
                            {t("codexAuth.paused")} · {t("codexAuth.resume")}
                          </span>
                        )}
                        {entry.active && !entry.disabled && (
                          <span className="badge badge-primary">{t("prov.accountActive")}</span>
                        )}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() =>
                          void authHandlers.onEditAlias(item.name, "api-key", entry.id, entry.label)
                        }
                      >
                        {t("prov.editAlias")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm pwi-auth-row-remove"
                        aria-label={`${t("common.remove")} — ${entry.label ?? entry.masked}`}
                        title={`${t("common.remove")} — ${entry.label ?? entry.masked}`}
                        onClick={() => void authHandlers.onRemoveApiKey(item.name, entry)}
                      >
                        <IconTrash style={{ width: 13, height: 13 }} aria-hidden="true" />
                      </button>
                    </div>
                    {capabilities ? (
                      <p className="muted">
                        {t("pws.dashboard.rateLimits")} · {migrationLabel}
                      </p>
                    ) : (
                      <div className="pwi-auth-acct-quota">
                        <ProviderAccountQuota
                          quotaMode={entry.quotaMode}
                          quota={entry.quota}
                          quotaUnavailable={entry.quotaUnavailable}
                          quotaPending={entry.quotaPending}
                          quotaFailure={entry.quotaFailure}
                        />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {keys.length === 0 &&
              (addingKey ? (
                <div className="pwi-auth-add-key">
                  <input
                    className="input"
                    type="password"
                    value={newKey}
                    onChange={(e) => setNewKey(e.target.value)}
                    placeholder={t("modal.apiKeyPlaceholder")}
                    autoComplete="off"
                    disabled={keyBusy}
                  />
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    onClick={() => void submitKey()}
                    disabled={keyBusy || !newKey.trim()}
                  >
                    {keyBusy ? t("pws.saving") : t("pws.addKey")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => {
                      setAddingKey(false);
                      setNewKey("");
                    }}
                  >
                    {t("common.cancel")}
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  style={{ marginTop: 8 }}
                  onClick={() => setAddingKey(true)}
                >
                  {t("pws.addKey")}
                </button>
              ))}
          </>
        )}
      </div>
    </section>
  );
}
