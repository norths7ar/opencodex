import { useState } from "react";
import { formatAccountPauseTooltip } from "../account-pause-display";
import { useI18n, useT } from "../i18n/shared";
import { IconPause, IconPlay, IconX } from "../icons";
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
import { AccountCreditsToggle, CreditsOnBadge } from "./CodexCreditSpend";
import CodexCreditsRow from "./CodexCreditsRow";
import QuotaBars from "./QuotaBars";
import { CodexPauseToggleLabel, CodexTicketBadge } from "./codex-account-pool-helpers";
import type { CodexAccountEntry } from "./codex-account-pool-types";
import { useCopyFeedback } from "./use-copy-feedback";
import { AccountMore, AccountRow, AccountSetting } from "./v2-accounts/account-row";
import { InlineBanner } from "./v2-accounts/surface";

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
  // Which rows have their ⋯ disclosure open; the order select mounts inside it on demand
  // unless the account already carries a non-default order (then it stays mounted, and the
  // order chip on the row says so).
  const [moreOpen, setMoreOpen] = useState<ReadonlySet<string>>(new Set());

  return (
    <>
      {pool.map((a) => {
        const showCredits = creditsVisible === true && a.credits !== undefined;
        const healthStatus = a.health?.status;
        const planExcluded = a.selectionExcludedReason === "plan_excluded";
        const showReauth = Boolean(a.needsReauth) || oauthHealthShowsReauth(healthStatus);
        const inCooldown = oauthHealthIsCooldown(healthStatus);
        const validationPending = a.health?.reason === "validation_pending";
        const healthLabel = formatOAuthHealthLabel(t, a.health);
        const healthSummary = formatOAuthHealthSummary(t, "codex", a.id, a.health);
        const next = isNext(a);
        const label = a.alias ?? a.email;
        const showDoctor = onCopyDoctor && oauthHealthShowsDoctor(healthStatus);
        return (
          <AccountRow
            key={a.id}
            active={next}
            paused={a.paused}
            dot={showReauth ? "warn" : a.paused ? "idle" : next ? "active" : "ok"}
            name={label}
            sub={a.alias ? a.email : undefined}
            chips={
              <>
                {next && !planExcluded && !showReauth && !inCooldown && !validationPending && (
                  <span className="badge badge-primary">{t("prov.accountActive")}</span>
                )}
                {a.plan && <span className="badge badge-plan">{a.plan}</span>}
                {planExcluded && (
                  <span
                    className="badge badge-muted"
                    title={t("codexAuth.planExcludedHint", {
                      plan: a.selectionExcludedPlan ?? a.plan ?? "",
                    })}
                  >
                    {t("codexAuth.planExcluded")}
                  </span>
                )}
                {a.paused && (
                  <span
                    className="badge badge-muted"
                    title={
                      a.pauseDetails
                        ? formatAccountPauseTooltip(t, locale, a.pauseDetails)
                        : t("codexAuth.pausedHint")
                    }
                  >
                    {t("codexAuth.paused")}
                  </span>
                )}

                <CreditsOnBadge enabled={a.creditsAfterLimit} />

                <CodexTicketBadge t={t} account={a} onClick={() => onOpenReset(a)} />
                {healthLabel && (
                  <span className={oauthHealthBadgeClass(healthStatus)}>{healthLabel}</span>
                )}
                {showReauth && !healthLabel && (
                  <span className="badge badge-amber">{t("codexAuth.needsReauth")}</span>
                )}
              </>
            }
            actions={
              <>
                {showReauth && (
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    onClick={() => onReauth(a.id)}
                  >
                    {t("codexAuth.reauthenticate")}
                  </button>
                )}
                <button
                  type="button"
                  className="acc-icon-btn codex-auth-action-btn"
                  onClick={() => onTogglePause(a)}
                  disabled={pauseBusy}
                  aria-busy={pauseUpdatingId === a.id || undefined}
                  title={a.paused ? t("codexAuth.pausedHint") : t("codexAuth.pause")}
                  aria-label={
                    a.paused
                      ? `${t("codexAuth.resume")}. ${t("codexAuth.pausedHint")}`
                      : t("codexAuth.pause")
                  }
                >
                  {a.paused ? <IconPlay width={15} /> : <IconPause width={15} />}
                  <span className="sr-only">
                    <CodexPauseToggleLabel
                      t={t}
                      paused={a.paused}
                      saving={pauseUpdatingId === a.id}
                    />
                  </span>
                </button>
              </>
            }
            more={
              /*
                Rarely used settings fold into a labelled disclosure (aria-expanded from the
                native details; controls revealed inline, DOM tab order — not a menu role).
                Switch/pause/reauth stay on the row: those are the daily decisions.
              */
              <AccountMore
                label={`${t("codexAuth.moreActions")} — ${a.email}`}
                title={t("codexAuth.moreActions")}
                open={moreOpen.has(a.id)}
                onToggle={(open) =>
                  setMoreOpen((prev) => {
                    const nextSet = new Set(prev);
                    if (open) nextSet.add(a.id);
                    else nextSet.delete(a.id);
                    return nextSet;
                  })
                }
              >
                {onToggleCreditsAfterLimit && (
                  <AccountSetting>
                    <AccountCreditsToggle
                      accountLabel={label}
                      enabled={a.creditsAfterLimit}
                      saving={creditsAfterLimitUpdatingId === a.id}
                      disabled={creditsAfterLimitUpdatingId !== null}
                      onChange={(enabled) => onToggleCreditsAfterLimit(a, enabled)}
                    />
                  </AccountSetting>
                )}
                <AccountSetting
                  label={
                    <span className="mono">
                      {t("prov.accountId")}: {displayAccountId(a.id)}
                    </span>
                  }
                >
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => idCopy.copy(a.id, a.id)}
                  >
                    {idCopy.outcomeFor(a.id) === "copied"
                      ? t("startup.copied")
                      : t("codexAuth.copyId")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => void onEditAlias(a)}
                  >
                    {t("prov.editAlias")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm acc-danger-btn"
                    aria-label={`${t("common.remove")} — ${a.email}`}
                    title={`${t("common.remove")} — ${a.email}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      void onRemove(a.id);
                    }}
                  >
                    <IconX width={14} /> {t("common.remove")}
                  </button>
                </AccountSetting>
              </AccountMore>
            }
          >
            {(healthSummary || inCooldown || showReauth) && (
              <div className="acc-row__notes">
                {healthSummary && (
                  <div className="card-sub faint">
                    <span>{healthSummary}</span>
                    {showDoctor && (
                      <button
                        type="button"
                        className="link-btn codex-auth-action-btn"
                        onClick={() => onCopyDoctor(a.id)}
                      >
                        <span aria-live="polite">
                          {doctorCopyButtonLabel(t, doctorCopyOutcomeFor?.(a.id))}
                        </span>
                      </button>
                    )}
                  </div>
                )}
                {!healthSummary && showDoctor && (
                  <div className="card-sub faint">
                    <button
                      type="button"
                      className="link-btn codex-auth-action-btn"
                      onClick={() => onCopyDoctor(a.id)}
                    >
                      <span aria-live="polite">
                        {doctorCopyButtonLabel(t, doctorCopyOutcomeFor?.(a.id))}
                      </span>
                    </button>
                  </div>
                )}
                {inCooldown && <div className="card-sub faint">{t("pws.healthCooldownHint")}</div>}
                {showReauth && <div className="card-sub faint">{t("codexAuth.tokenExpired")}</div>}
              </div>
            )}
            {!showReauth && !inCooldown && (
              <div className="acc-row__meters">
                <QuotaBars
                  threshold={0}
                  quota={a.quota}
                  plan={a.plan}
                  t={t}
                  pending={a.quota == null && (loading || !showCredits)}
                  afterWeekly={
                    showCredits && !loading ? (
                      <CodexCreditsRow credits={a.credits} t={t} locale={locale} />
                    ) : undefined
                  }
                />
              </div>
            )}
          </AccountRow>
        );
      })}
    </>
  );
}

export function CodexAccountPoolReauthBanner({ onReauth }: { onReauth: () => void }) {
  const t = useT();
  return (
    <InlineBanner
      tone="warn"
      className="acc-row-banner"
      action={
        <button type="button" className="btn btn-primary btn-sm" onClick={onReauth}>
          {t("codexAuth.reauthenticate")}
        </button>
      }
    >
      {t("codexAuth.tokenExpired")}
    </InlineBanner>
  );
}
