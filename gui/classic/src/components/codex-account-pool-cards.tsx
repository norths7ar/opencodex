import { useState } from "react";
import { formatAccountPauseTooltip } from "../account-pause-display";
import { useI18n,useT } from "../i18n/shared";
import { IconAlert,IconPause,IconPlay,IconX } from "../icons";
import { displayAccountId } from "../lib/privacy";
import {
doctorCopyButtonLabel,
formatOAuthHealthLabel,
formatOAuthHealthSummary,
oauthHealthBadgeClass,
oauthHealthIsCooldown,
oauthHealthShowsDoctor,
oauthHealthShowsReauth,
} from "../oauth-health-display";
import { AccountCreditsToggle,CreditsOnBadge } from "./CodexCreditSpend";
import CodexCreditsRow from "./CodexCreditsRow";
import QuotaBars from "./QuotaBars";
import { CodexPauseToggleLabel,CodexTicketBadge } from "./codex-account-pool-helpers";
import type { CodexAccountEntry } from "./codex-account-pool-types";
import { useCopyFeedback } from "./use-copy-feedback";

export function CodexAccountPoolCards({
  pool,
  creditsVisible,
  loading = false,
  activeId,
  onOpenReset,
  onTogglePause,
  pauseUpdatingId,
  pauseBusy,
  onReauth,
  onEditAlias,
  onRemove,
  onToggleCreditsAfterLimit,
  creditsAfterLimitUpdatingId = null,
  onCopyDoctor,
  doctorCopyOutcomeFor,
}: {
  pool: CodexAccountEntry[];
  creditsVisible?: boolean;
  loading?: boolean;
  activeId: string | null;
  onOpenReset: (account: CodexAccountEntry) => void;
  onTogglePause: (account: CodexAccountEntry) => void;
  pauseUpdatingId: string | null;
  pauseBusy: boolean;
  onReauth: (id: string) => void;
  onEditAlias: (account: CodexAccountEntry) => void;
  onRemove: (id: string) => void;
  /** Writes one account's "use credits after limit" switch, shown in its "more" disclosure. */
  onToggleCreditsAfterLimit?: (account: CodexAccountEntry, enabled: boolean) => void;
  creditsAfterLimitUpdatingId?: string | null;
  onCopyDoctor?: (accountId: string) => void;
  doctorCopyOutcomeFor?: (accountId: string) => "copied" | "unavailable" | null;
}) {
  const t = useT();
  const { locale } = useI18n();
  const isNext = (account: CodexAccountEntry) => !account.paused && activeId === account.id;
  const idCopy = useCopyFeedback<string>();
  // Which cards have their ⋯ disclosure open; the priority select renders inside it unless
  // the account already carries a non-default priority (then it stays inline).
  const [moreOpen, setMoreOpen] = useState<ReadonlySet<string>>(new Set());

  return (
    <>
      {pool.map(a => {
        const showCredits = creditsVisible === true && a.credits !== undefined;
        const healthStatus = a.health?.status;
        const planExcluded = a.selectionExcludedReason === "plan_excluded";
        const showReauth = Boolean(a.needsReauth) || oauthHealthShowsReauth(healthStatus);
        const inCooldown = oauthHealthIsCooldown(healthStatus);
        const validationPending = a.health?.reason === "validation_pending";
        const healthLabel = formatOAuthHealthLabel(t, a.health);
        const healthSummary = formatOAuthHealthSummary(t, "codex", a.id, a.health);
        return (
        <div key={a.id} className={`card ${isNext(a) ? "card-active" : ""}`} style={{ marginBottom: 8 }}>
          <div className="card-head">
            <span className={`dot ${showReauth ? "dot-amber" : isNext(a) ? "dot-blue" : "dot-muted"}`} />
            <strong>{a.alias ?? a.email}</strong>
            <span className="card-badges">
              {a.plan && <span className="badge badge-green">{a.plan}</span>}
              {planExcluded && (
                <span className="badge badge-muted" title={t("codexAuth.planExcludedHint", { plan: a.selectionExcludedPlan ?? a.plan ?? "" })}>
                  {t("codexAuth.planExcluded")}
                </span>
              )}
              {a.paused && (
                <span className="badge badge-muted" title={a.pauseDetails ? formatAccountPauseTooltip(t, locale, a.pauseDetails) : t("codexAuth.pausedHint")}>
                  {t("codexAuth.paused")}
                </span>
              )}

              <CreditsOnBadge enabled={a.creditsAfterLimit} />

              <CodexTicketBadge t={t} account={a} onClick={() => onOpenReset(a)} />
              {healthLabel && (
                <span className={oauthHealthBadgeClass(healthStatus)}>{healthLabel}</span>
              )}
              {showReauth && !healthLabel && <span className="badge badge-amber">{t("codexAuth.needsReauth")}</span>}
              {isNext(a) && !planExcluded && !showReauth && !inCooldown && !validationPending && (
                <span className="badge badge-primary">
                  {t("prov.accountActive")}
                </span>
              )}
            </span>

            {showReauth && (
              <button type="button" className="btn btn-primary btn-sm" onClick={() => onReauth(a.id)}>
                {t("codexAuth.reauthenticate")}
              </button>
            )}
            {onCopyDoctor && oauthHealthShowsDoctor(healthStatus) && (
              <button type="button" className="btn btn-ghost btn-sm codex-auth-action-btn" onClick={() => onCopyDoctor(a.id)}>
                <span aria-live="polite">{doctorCopyButtonLabel(t, doctorCopyOutcomeFor?.(a.id))}</span>
              </button>
            )}
            <button
              type="button"
              className="btn btn-sm btn-ghost codex-auth-action-btn"
              onClick={() => onTogglePause(a)}
              disabled={pauseBusy}
              title={a.paused ? t("codexAuth.pausedHint") : undefined}
              aria-label={a.paused ? `${t("codexAuth.resume")}. ${t("codexAuth.pausedHint")}` : t("codexAuth.pause")}
            >
              {a.paused ? <IconPlay width={14} /> : <IconPause width={14} />}
              <CodexPauseToggleLabel
                t={t}
                paused={a.paused}
                saving={pauseUpdatingId === a.id}
              />
            </button>
            {/*
              Rarely used actions fold into a labelled disclosure (aria-expanded from the
              native details; controls revealed inline, DOM tab order — not a menu role).
              Switch/pause/reauth stay inline: those are the daily decisions.
            */}
            <details
              className="codex-account-more card-right"
              open={moreOpen.has(a.id)}
              onToggle={e => {
                const open = (e.currentTarget as HTMLDetailsElement).open;
                setMoreOpen(prev => { const next = new Set(prev); if (open) next.add(a.id); else next.delete(a.id); return next; });
              }}
            >
              <summary className="btn btn-ghost btn-sm" aria-label={`${t("codexAuth.moreActions")} — ${a.email}`} title={t("codexAuth.moreActions")}>⋯</summary>
              <div className="codex-account-more-body">
                <span className="mono text-caption muted">{t("prov.accountId")}: {displayAccountId(a.id)}</span>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => idCopy.copy(a.id, a.id)}>
                  {idCopy.outcomeFor(a.id) === "copied" ? t("startup.copied") : t("codexAuth.copyId")}
                </button>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => void onEditAlias(a)}>
                  {t("prov.editAlias")}
                </button>
                {onToggleCreditsAfterLimit && (
                  <AccountCreditsToggle
                    accountLabel={a.alias ?? a.email}
                    enabled={a.creditsAfterLimit}
                    saving={creditsAfterLimitUpdatingId === a.id}
                    disabled={creditsAfterLimitUpdatingId !== null}
                    onChange={enabled => onToggleCreditsAfterLimit(a, enabled)}
                  />
                )}
                <button
                  type="button"
                  className="btn-icon btn-icon-danger"
                  aria-label={`${t("common.remove")} — ${a.email}`}
                  title={`${t("common.remove")} — ${a.email}`}
                  onClick={e => { e.stopPropagation(); void onRemove(a.id); }}
                >
                  <IconX width={14} />
                </button>
              </div>
            </details>
          </div>
          <div className="codex-account-identity">
            <div className="codex-account-identity-copy">{a.email}{a.plan ? ` · ${a.plan}` : ""}</div>
            <div className="codex-account-controls">


            </div>
          </div>
          {healthSummary && (
            <div className="card-sub faint">{healthSummary}</div>
          )}
          {inCooldown && (
            <div className="card-sub faint">{t("pws.healthCooldownHint")}</div>
          )}
          {showReauth
            ? <div className="card-sub faint">{t("codexAuth.tokenExpired")}</div>
            : !inCooldown && <>
                <QuotaBars threshold={0}
                  quota={a.quota}
                  plan={a.plan}
                  t={t}
                  pending={a.quota == null && (loading || !showCredits)}
                  afterWeekly={showCredits && !loading ? <CodexCreditsRow credits={a.credits} t={t} locale={locale} /> : undefined}
                />
              </>}
        </div>
        );
      })}
    </>
  );
}

export function CodexAccountPoolReauthBanner({
  onReauth,
}: {
  onReauth: () => void;
}) {
  const t = useT();
  return (
    <div className="notice-warn" style={{ marginBottom: 12, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
      <span><IconAlert width={14} /> {t("codexAuth.tokenExpired")}</span>
      <button type="button" className="btn btn-primary btn-sm" onClick={onReauth}>
        {t("codexAuth.reauthenticate")}
      </button>
    </div>
  );
}
