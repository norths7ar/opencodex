import type { ReactNode } from "react";
import { navigateHash } from "../hash-routing";
import { hardLockThresholds } from "../hooks/useCodexAccountPool";
import type { TFn } from "../i18n/shared";
import { useI18n } from "../i18n/shared";
import { IconLock,IconPause,IconPlay,IconPlus,IconRefresh,IconTicket } from "../icons";
import {
doctorCopyButtonLabel,
formatOAuthHealthLabel,
formatOAuthHealthSummary,
oauthHealthBadgeClass,
oauthHealthIsCooldown,
oauthHealthShowsDoctor,
oauthHealthShowsReauth,
} from "../oauth-health-display";
import type { NoticeTone } from "../ui";
import { CodexPauseToggleLabel,CodexTicketBadge } from "./codex-account-pool-helpers";
import type { CodexAccountEntry,CodexAccountLoadState } from "./codex-account-pool-types";
import { AccountCreditsToggle,CreditsOnBadge } from "./CodexCreditSpend";
import CodexCreditsRow from "./CodexCreditsRow";
import { LoginHint } from "./login-url-block";
import QuotaBars from "./QuotaBars";
import type { MainDeviceReauthState } from "./use-main-device-reauth";

export function CodexAccountPoolMainCard({
  t,
  main,
  isMainActive,
  onTogglePause,
  pauseUpdatingId,
  pauseBusy,
  onOpenReset,
  onCopyDoctor,
  doctorCopyOutcomeFor,
  onManageMainHardLock,
  mainReauth,
  onToggleCreditsAfterLimit,
  creditsAfterLimitUpdatingId = null,
  creditsVisible,
  loading = false,
}: {
  t: TFn;
  main: CodexAccountEntry | undefined;
  creditsVisible?: boolean;
  loading?: boolean;
  isMainActive: boolean;
  onTogglePause: (entry: CodexAccountEntry) => void;
  pauseUpdatingId: string | null;
  pauseBusy: boolean;
  onOpenReset: (account: CodexAccountEntry) => void;
  /** Writes the main login's "use credits after limit" switch, shown in its "more" disclosure. */
  onToggleCreditsAfterLimit?: (entry: CodexAccountEntry, enabled: boolean) => void;
  creditsAfterLimitUpdatingId?: string | null;
  onCopyDoctor?: (accountId: string) => void;
  doctorCopyOutcomeFor?: (accountId: string) => "copied" | "unavailable" | null;
  onManageMainHardLock?: () => void;
  /** #3898: native-main device reauth flow state and controls (dedicated namespace). */
  mainReauth?: {
    state: MainDeviceReauthState;
    start: () => Promise<void>;
    cancel: () => Promise<void>;
  } | undefined;
}) {
  const { locale } = useI18n();
  const showCredits = creditsVisible === true && main?.credits !== undefined;
  const mainFallbackLabel = t("codexAuth.codexApp");
  const mainId = main?.id ?? "__main__";
  const mainSwitchEntry: CodexAccountEntry = {
    id: "__main__",
    email: main?.email || mainFallbackLabel,
    plan: main?.plan,
    isMain: true,
    paused: main?.paused ?? false,
    priority: main?.priority ?? 0,
    autoSwitchThresholdOverride: main?.autoSwitchThresholdOverride ?? null,
    hasCredential: true,
    quota: main?.quota ?? null,
    quotaAutoRefresh: main?.quotaAutoRefresh ?? {
      fiveHourAvailable: false,
      weeklyAvailable: false,
      fiveHourEnabled: false,
      weeklyEnabled: false,
    },
  };
  const showReauth = Boolean(main?.needsReauth) || oauthHealthShowsReauth(main?.health?.status);
  const inCooldown = oauthHealthIsCooldown(main?.health?.status);
  const policy = main?.mainAccountHardLock;
  const hardLocked = policy?.enabled === true && policy.state === "blocked";
  const healthLabel = formatOAuthHealthLabel(t, main?.health);
  const healthSummary = main
    ? formatOAuthHealthSummary(t, "codex", mainId, main.health)
    : null;

  return (
    <div className={`card ${isMainActive && !hardLocked ? "card-active" : ""}`} style={{ marginBottom: 12 }}>
      <div className="card-head">
        <span className={`dot ${showReauth || hardLocked ? "dot-amber" : "dot-green"}`} />
        <strong>{t("codexAuth.mainAccount")}</strong>
        <span className="card-badges">
          {main?.plan && <span className="badge badge-green">{main.plan}</span>}
          {main?.paused && (
            <span className="badge badge-muted" title={t("codexAuth.pausedHint")}>
              {t("codexAuth.paused")}
            </span>
          )}

          <CreditsOnBadge enabled={main?.creditsAfterLimit} />

          {main && <CodexTicketBadge t={t} account={{ ...main, id: "__main__" } as CodexAccountEntry} onClick={() => onOpenReset({ ...main, id: "__main__" } as CodexAccountEntry)} />}
          {healthLabel && (
            <span className={oauthHealthBadgeClass(main?.health?.status)}>{healthLabel}</span>
          )}
          {showReauth && !healthLabel && <span className="badge badge-amber">{t("codexAuth.needsReauth")}</span>}
          {!main?.paused && !hardLocked && (
            <span className={`badge ${isMainActive ? "badge-primary" : "badge-muted"}`}>
              {isMainActive
                ? t("prov.accountActive")
                : t("codexAuth.current")}
            </span>
          )}
        </span>

        {onCopyDoctor && oauthHealthShowsDoctor(main?.health?.status) && (
          <button type="button" className="btn btn-ghost btn-sm codex-auth-action-btn" onClick={() => onCopyDoctor(mainId)}>
            <span aria-live="polite">{doctorCopyButtonLabel(t, doctorCopyOutcomeFor?.(mainId))}</span>
          </button>
        )}
        {main && (
          <button
            type="button"
            className="btn btn-sm btn-ghost codex-auth-action-btn"
            onClick={() => onTogglePause(mainSwitchEntry)}
            disabled={pauseBusy}
            title={main.paused ? t("codexAuth.pausedHint") : undefined}
            aria-label={main.paused ? `${t("codexAuth.resume")}. ${t("codexAuth.pausedHint")}` : t("codexAuth.pause")}
          >
            {main.paused ? <IconPlay width={14} /> : <IconPause width={14} />}
            <CodexPauseToggleLabel
              t={t}
              paused={!!main.paused}
              saving={pauseUpdatingId === "__main__"}
            />
          </button>
        )}
        {/* Same disclosure as the pool cards' "more" actions; the main login only carries its
            credits switch there. */}
        {main?.hasCredential && onToggleCreditsAfterLimit && (
          <details className="codex-account-more card-right">
            <summary className="btn btn-ghost btn-sm" aria-label={`${t("codexAuth.moreActions")} — ${t("codexAuth.mainAccount")}`} title={t("codexAuth.moreActions")}>⋯</summary>
            <div className="codex-account-more-body">
              <AccountCreditsToggle
                accountLabel={main.alias ?? (main.email || t("codexAuth.mainAccount"))}
                enabled={main.creditsAfterLimit}
                saving={creditsAfterLimitUpdatingId === "__main__"}
                disabled={creditsAfterLimitUpdatingId !== null}
                hint={t("codexAuth.creditsAfterLimitMainHint")}
                onChange={enabled => onToggleCreditsAfterLimit(mainSwitchEntry, enabled)}
              />
            </div>
          </details>
        )}
        <span className="card-right"><IconLock width={14} /> {t("codexAuth.appLogin")}</span>
      </div>
      <div className="codex-account-identity">
        <div className="codex-account-identity-copy">{main?.email || t("codexAuth.appLogin")}{main?.plan ? ` · ${main.plan}` : ""}</div>
        {/* The main card keeps its order select inline: it is one control, not one per pool row,
            and the main card has no ⋯ disclosure to fold it into. */}
        {main && (
          <div className="codex-account-controls">


          </div>
        )}
      </div>
      {policy?.enabled && (
        <div className={`codex-main-hard-lock-status${hardLocked ? " is-blocked" : ""}`}>
          <p role="status">{t(hardLocked ? "codexAuth.mainHardLockBlocked"
            : policy.state === "ready" ? "codexAuth.mainHardLockMonitoring" : "codexAuth.mainHardLockUnknown", hardLockThresholds(policy.thresholds))}</p>
          {onManageMainHardLock
            ? <button type="button" className="link-btn" onClick={onManageMainHardLock}>{t("codexAuth.mainHardLockManage")}</button>
            : <button type="button" className="link-btn" onClick={() => navigateHash("codex-set")}>{t("codexAuth.mainHardLockManage")}</button>}
        </div>
      )}
      {policy?.externalUsage && <div className="codex-main-hard-lock-status is-blocked"><p role="status">{t("codexAuth.mainExternalUsageWarning")}</p></div>}
      {healthSummary && (
        <div className="card-sub faint">{healthSummary}</div>
      )}
      {inCooldown && (
        <div className="card-sub faint">{t("pws.healthCooldownHint")}</div>
      )}
      {showReauth
        ? <div className="card-sub faint">
            <p role="status">{t("codexAuth.mainTokenExpired")}</p>
            {mainReauth && (mainReauth.state.phase === "idle" || mainReauth.state.phase === "failed" || mainReauth.state.phase === "cancelled") && (
              <>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm codex-auth-action-btn"
                  onClick={() => { void mainReauth.start(); }}
                >
                  {t("codexAuth.mainReauthDevice")}
                </button>
                {mainReauth.state.phase === "failed" && (
                  <span className="badge badge-amber">{t("codexAuth.mainReauthFailed")}: {mainReauth.state.code}</span>
                )}
              </>
            )}
            {mainReauth && mainReauth.state.phase === "starting" && (
              <span className="faint">{t("codexAuth.mainReauthPending")}</span>
            )}
            {mainReauth && (mainReauth.state.phase === "pending" || mainReauth.state.phase === "committing") && (
              <span className="codex-main-reauth-pending">
                {/* The shared renderer every other login surface uses: a copyable code, a
                    selectable and copyable URL, and one click to copy the code and open the
                    page. Plain text here left the user retyping both by hand. */}
                <LoginHint hint={{ url: mainReauth.state.verificationUrl, deviceCode: mainReauth.state.deviceCode }} />
                <span className="faint">{t("codexAuth.mainReauthPending")}</span>
                {mainReauth.state.cancelFailed && (
                  <span role="status" className="badge badge-amber">{t("codexAuth.mainReauthFailed")}</span>
                )}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm codex-auth-action-btn"
                  onClick={() => { void mainReauth.cancel(); }}
                >
                  {t("codexAuth.mainReauthCancel")}
                </button>
              </span>
            )}
            {mainReauth && mainReauth.state.phase === "succeeded" && (
              <span className="badge badge-primary">{t("codexAuth.mainReauthSucceeded")}</span>
            )}
          </div>
        : !inCooldown && <>
            <QuotaBars threshold={0}
              quota={main?.quota ?? null}
              plan={main?.plan}
              t={t}
              pending={main != null && main.quota == null && (loading || !showCredits)}
              afterWeekly={showCredits && !loading ? <CodexCreditsRow credits={main?.credits} t={t} locale={locale} /> : undefined}
            />
          </>}
    </div>
  );
}

export function CodexAccountPoolPageHead({
  t,
  embedded,
  refreshingQuota,
  pauseBusy,
  actionFeedback,
  actionFeedbackTone,
  onRefresh,
  creditsVisible,
  creditsBusy,
  onToggleCredits,
  creditSpendControl,
}: {
  t: TFn;
  embedded: boolean;
  /** Undefined until settings loads. */
  creditsVisible?: boolean;
  creditsBusy?: boolean;
  onToggleCredits?: () => void;
  /** The global "use credits" switch, rendered beside the credits display switch. */
  creditSpendControl?: ReactNode;
  refreshingQuota: boolean;
  pauseBusy?: boolean;
  actionFeedback?: string | null;
  actionFeedbackTone?: NoticeTone | null;
  onRefresh: () => void;
}) {
  return (
    <div
      className={embedded ? "row" : "page-head codex-auth-page-head"}
      style={embedded ? { justifyContent: "flex-end", marginBottom: 8 } : undefined}
    >
      {!embedded && <h2 className="page-title">{t("nav.codexAuth")}</h2>}
      <div className={embedded ? "row" : "codex-auth-page-head__actions"}>
        <span
          className={`codex-auth-page-head__feedback${actionFeedbackTone === "ok" ? " is-ok" : ""}${actionFeedbackTone === "warn" ? " is-warn" : ""}${actionFeedbackTone === "err" ? " is-err" : ""}`}
          role="status"
          aria-live="polite"
        >
          {actionFeedback ?? ""}
        </span>
        {creditsVisible !== undefined && onToggleCredits && (
          <span className="codex-auth-credits-toggle">
            <span className="codex-auth-credits-toggle__label">{t("codexAuth.creditsToggle")}</span>
            <button
              type="button"
              className={`toggle ${creditsVisible ? "on" : ""}`}
              onClick={onToggleCredits}
              disabled={!!creditsBusy}
              aria-pressed={creditsVisible}
              aria-label={t("codexAuth.creditsToggle")}
              title={t("codexAuth.creditsToggleHint")}
            >
              <span className="toggle-knob" />
            </button>
          </span>
        )}
        {creditSpendControl}
        {/* The standalone pause/refresh row sits next to the account cards. Embedded
            surfaces keep those actions beside feedback because there is no page title. */}
        {embedded && (
          <CodexAccountPoolActionButtons
            t={t}
            refreshingQuota={refreshingQuota}
            pauseBusy={pauseBusy}
            onRefresh={onRefresh}
          />
        )}
      </div>
    </div>
  );
}

/** The pause/refresh pair, shared by the embedded head and the standalone action row. */
export function CodexAccountPoolActionButtons({
  t,
  refreshingQuota,
  pauseBusy,
  onRefresh,
}: {
  t: TFn;
  refreshingQuota: boolean;
  pauseBusy?: boolean;
  onRefresh: () => void;
}) {
  return (
    <>
      <button
        type="button"
        className="btn btn-sm btn-ghost codex-auth-action-btn"
        onClick={onRefresh}
        disabled={refreshingQuota || !!pauseBusy}
      >
        <IconRefresh width={14} /> {refreshingQuota ? t("codexAuth.refreshingQuota") : t("codexAuth.refreshQuota")}
      </button>
    </>
  );
}

/**
 * Standalone-page action row: the pause/refresh pair, moved out of the page head and
 * placed directly above the account cards they operate on.
 */
export function CodexAccountPoolActions(props: {
  t: TFn;
  refreshingQuota: boolean;
  pauseBusy?: boolean;
  onRefresh: () => void;
}) {
  return (
    <div className="codex-auth-actions-row">
      <CodexAccountPoolActionButtons {...props} />
    </div>
  );
}

export function CodexAccountPoolLoadStates({
  t,
  loadState,
  refreshFailed,
  accountsCount,
  onRetry,
}: {
  t: TFn;
  loadState: CodexAccountLoadState;
  refreshFailed: boolean;
  accountsCount: number;
  onRetry: () => void;
}): ReactNode {
  // Cold start with no seeded accounts: mirror the ready layout (main card + pool
  // section + empty) so Auto-switch / strategy do not jump when real nodes mount.
  // Caller places this below the stable account-mode banner.
  if (loadState === "loading" && accountsCount === 0) {
    return (
      <div className="codex-auth-load-skeleton" role="status" aria-live="polite" aria-busy="true">
        {/* Match ready MainCard chrome (badge + pause + app-login) so head height does not jump. */}
        <div className="card codex-auth-load-skeleton__main" style={{ marginBottom: 12 }} aria-hidden="true">
          <div className="card-head">
            <span className="dot dot-muted" />
            <strong>{t("codexAuth.mainAccount")}</strong>
            <span className="card-badges">
              <span className="badge badge-muted codex-ticket-badge-slot" aria-hidden="true">
                <IconTicket width={12} />0
              </span>
              <span className="badge badge-primary">{t("codexAuth.nextSession")}</span>
            </span>
            <button type="button" className="btn btn-sm btn-ghost" tabIndex={-1} disabled>
              <IconPause width={14} /> {t("codexAuth.pause")}
            </button>
            <span className="card-right"><IconLock width={14} /> {t("codexAuth.appLogin")}</span>
          </div>
          <div className="card-sub">
            {/* Strut keeps the sub line-box equal to ready email/plan text; shimmer is visual only. */}
            <span className="codex-auth-load-skeleton__strut">{t("codexAuth.appLogin")}</span>
            <span className="codex-auth-load-skeleton__line codex-auth-load-skeleton__line--sub" />
          </div>
          <QuotaBars threshold={0} quota={null} t={t} pending />
        </div>
        <div className="section-sep" aria-hidden="true">
          <span className="section-label">{t("codexAuth.accountPool")}</span>
          <div className="sep-line" />
          {/* Same Add control as ready section-sep (inert) so the row height matches. */}
          <button type="button" className="btn btn-sm btn-ghost" tabIndex={-1} disabled>
            <IconPlus width={14} /> {t("prov.login")}
          </button>
        </div>
        <div className="empty codex-auth-pool-empty codex-auth-load-skeleton__empty" aria-hidden="true">
          <div className="title">{t("codexAuth.noPool")}</div>
        </div>
        <span className="sr-only">{t("pws.accountsLoading")}</span>
      </div>
    );
  }
  if (loadState === "error") {
    return (
      <div className="pwi-auth-state pwi-auth-state--error" role="alert">
        <span>{t("codexAuth.loadFailed")}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>{t("pws.retryAccounts")}</button>
      </div>
    );
  }
  // Rows survived a failed refresh, so they are still worth showing — but they are the ones from
  // before it, and an account added since is simply not among them. A status rather than an alert:
  // nothing on screen is wrong, it is just older than it looks.
  if (refreshFailed && accountsCount > 0) {
    return (
      <div className="pwi-auth-state pwi-auth-state--stale" role="status">
        <span>{t("codexAuth.accountsRefreshFailed")}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>{t("pws.retryAccounts")}</button>
      </div>
    );
  }
  return null;
}
