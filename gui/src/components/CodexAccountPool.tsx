import type { ReactNode } from "react";
import { useCallback,useEffect,useRef,useState } from "react";
import { confirmAction,requestTextValue } from "../action-dialogs";
import { createBoundedFetch,type BoundedFetch } from "../bounded-fetch";
import type { CodexAccountMutationCompletion } from "../codex-account-mutation";
import { creditSpendSummary } from "../codex-credit-spend";
import type { CodexAccountModeState } from "../codex-multi-state";
import { quotaActivationWindows,readQuotaActivationSettings,type QuotaAutoRefreshSettings } from "../codex-quota-activation";
import { CREDENTIAL_ALIAS_MAX_LENGTH,credentialAliasRejection } from "../credential-alias";
import { readJsonIfOk } from "../fetch-json";
import { navigateHash } from "../hash-routing";
import { useCodexAccountPool,type CodexAccountPoolController } from "../hooks/useCodexAccountPool";
import { useCodexCreditsVisibility } from "../hooks/useCodexCreditsVisibility";
import { useT } from "../i18n/shared";
import { IconPlus } from "../icons";
import { accountNeedsReauth } from "../oauth-health-display";
import type { NoticeTone } from "../ui";
import AddCodexAccountModal from "./AddCodexAccountModal";
import { CodexAccountPoolCards,CodexAccountPoolReauthBanner } from "./codex-account-pool-cards";
import { redeemResetCredit } from "./codex-account-pool-handlers";
import { CodexAccountPoolActions,CodexAccountPoolAddButton,CodexAccountPoolLoadStates,CodexAccountPoolMainCard,CodexAccountPoolPageHead } from "./codex-account-pool-main-card";
import type { CodexAccountEntry } from "./codex-account-pool-types";
import { CodexAccountResetModal } from "./codex-account-reset-modal";
import CodexAuthAdvancedSettings from "./CodexAuthAdvancedSettings";
import { CodexCreditSpendSwitch } from "./CodexCreditSpend";
import CodexQuotaAutoRefreshSetting from "./CodexQuotaAutoRefreshSetting";
import ProviderModelsNotice from "./ProviderModelsNotice";
import { useCopyFeedback } from "./use-copy-feedback";
import { useMainDeviceReauth } from "./use-main-device-reauth";
import { Panel } from "./v2-accounts/surface";

// Single definition lives with the controller that owns this data (WP3).
export type { CodexAccountEntry } from "../hooks/useCodexAccountPool";

const DOCTOR_CMD = "ocx doctor";

/** The selected ChatGPT subscription account, shared by settings and Providers. */
export default function CodexAccountPool({ apiBase, banner = null, embedded = false, onActiveNeedsReauthChange, controller: injectedController, advancedExtras = null, hasMainHardLockSetting = false }: {
  apiBase: string;
  accountModeState?: CodexAccountModeState | null;
  banner?: ReactNode;
  embedded?: boolean;
  onActiveNeedsReauthChange?: (needs: boolean) => void;
  /** Whole boxes rendered inside Advanced settings. Never fold these internally. */
  advancedExtras?: ReactNode;
  /** This surface supplies the protection setting in advancedExtras; manage opens it locally. */
  hasMainHardLockSetting?: boolean;
  /**
   * WP3: when Providers owns the controller, every surface shares one instance so a
   * mutation on Overview is immediately visible on the Accounts tab. The standalone
   * Codex Auth page passes nothing and gets its own.
   */
  controller?: CodexAccountPoolController;
}) {
  const t = useT();
  // A hook cannot be called conditionally, so the fallback instance is always created
  // but stays inert (no load, no polling) whenever a shared controller was injected.
  const ownController = useCodexAccountPool(apiBase, !injectedController);
  const controller = injectedController ?? ownController;
  const { accounts: storedAccounts, activeId, loadState, refreshFailed, pauseUpdatingId, load } = controller;
  const accounts = storedAccounts.filter(account => activeId && activeId !== "__main__" ? account.id === activeId : account.isMain);
  // #3898: the native-main device reauth drives the dedicated namespace; a
  // completed flow refreshes the account list so the card leaves reauth state.
  const mainReauth = useMainDeviceReauth(apiBase, () => { void load(); });
  const mainReauthActive = mainReauth.state.phase === "starting"
    || mainReauth.state.phase === "pending"
    || mainReauth.state.phase === "committing";
  const [showAdd, setShowAdd] = useState(false);
  const [modelsNotice, setModelsNotice] = useState<{ catalogRefreshPending: boolean } | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const hardLockFocusPending = useRef(false);
  const focusHardLockSetting = useCallback(() => {
    const target = document.getElementById("codex-main-hard-lock-setting");
    target?.focus();
    target?.scrollIntoView({ block: "nearest" });
  }, []);
  useEffect(() => {
    if (advancedOpen && hardLockFocusPending.current) {
      hardLockFocusPending.current = false;
      focusHardLockSetting();
    }
  }, [advancedOpen, focusHardLockSetting]);
  const manageMainHardLock = () => {
    if (advancedOpen) focusHardLockSetting();
    else { hardLockFocusPending.current = true; setAdvancedOpen(true); }
  };
  const [reauthId, setReauthId] = useState<string | null>(null);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);
  const [actionFeedbackTone, setActionFeedbackTone] = useState<NoticeTone | null>(null);
  const feedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [refreshingQuota, setRefreshingQuota] = useState(false);
  const [quotaBusyScope, setQuotaBusyScope] = useState<string | null>(null);
  const [quotaState, setQuotaState] = useState<{
    apiBase: string; revision: number; settings: QuotaAutoRefreshSettings | null; error: boolean;
  } | null>(null);
  const quotaAutoRefreshMutationRevisionRef = useRef(0);
  const quotaScopeRef = useRef<AbortController | null>(null);
  const quotaMutationRef = useRef<BoundedFetch | null>(null);
  const [quotaReadRevision, setQuotaReadRevision] = useState(0);
  const [quotaOrigin, setQuotaOrigin] = useState(apiBase);
  const [quotaFeedback, setQuotaFeedback] = useState<{ apiBase: string; message: string; failed: boolean } | null>(null);
  const failedQuotaTarget = useRef<{ apiBase: string; enabled: boolean } | null>(null);
  const quotaCurrent = quotaState?.apiBase === apiBase && quotaState.revision === quotaReadRevision ? quotaState : null;
  const quotaAutoRefreshSettings = quotaCurrent?.settings ?? null;
  const quotaLoadError = quotaCurrent?.error ?? false;
  const quotaAutoRefreshBusy = quotaBusyScope === apiBase;
  // Adjust the snapshot at the prop boundary, not in an effect: returning to a
  // previously visited proxy must not revive its old settings before the new GET.
  if (quotaOrigin !== apiBase) {
    setQuotaOrigin(apiBase);
    setQuotaReadRevision(value => value + 1);
    setQuotaState(null);
    setQuotaBusyScope(null);
    setQuotaFeedback(null);
  }
  const [resetPopup, setResetPopup] = useState<CodexAccountEntry | null>(null);
  const [resetConfirm, setResetConfirm] = useState(false);
  const [redeeming, setRedeeming] = useState(false);
  const [creditDetails, setCreditDetails] = useState<{ granted_at: string; expires_at: string }[] | null>(null);
  const [creditDetailsLoading, setCreditDetailsLoading] = useState(false);
  const doctorCopy = useCopyFeedback<string>();

  const showActionFeedback = useCallback((text: string, tone: NoticeTone = "ok") => {
    if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
    setActionFeedback(text);
    setActionFeedbackTone(tone);
    feedbackTimerRef.current = setTimeout(() => {
      setActionFeedback(null);
      setActionFeedbackTone(null);
      feedbackTimerRef.current = null;
    }, 5000);
  }, []);

  useEffect(() => () => {
    if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
  }, []);

  const credits = useCodexCreditsVisibility(apiBase, load, showActionFeedback, t, {
    revision: quotaReadRevision,
    onRead: (read, signal) => {
      const mutationRevision = quotaAutoRefreshMutationRevisionRef.current;
      read.then(payload => {
        if (signal.aborted || quotaAutoRefreshMutationRevisionRef.current !== mutationRevision) return;
        setQuotaState({ apiBase, revision: quotaReadRevision, settings: readQuotaActivationSettings(payload), error: false });
        setQuotaBusyScope(null);
      }).catch(() => {
        if (signal.aborted || quotaAutoRefreshMutationRevisionRef.current !== mutationRevision) return;
        setQuotaState({ apiBase, revision: quotaReadRevision, settings: null, error: true });
        setQuotaBusyScope(null);
      });
    },
  });

  const copyDoctor = useCallback((accountId: string) => {
    doctorCopy.copy(DOCTOR_CMD, accountId);
  }, [doctorCopy]);

  useEffect(() => {
    if (!showAdd && !mainReauthActive) return;
    const token = controller.pauseRefresh();
    return () => controller.resumeRefresh(token);
  }, [controller, showAdd, mainReauthActive]);

  const activePoolAccount = activeId && activeId !== "__main__"
    ? accounts.find(a => a.id === activeId)
    : null;
  const activePoolNeedsReauth = !activePoolAccount?.paused && accountNeedsReauth(activePoolAccount);

  useEffect(() => {
    onActiveNeedsReauthChange?.(activePoolNeedsReauth);
  }, [activePoolNeedsReauth, onActiveNeedsReauthChange]);

  const openReauth = useCallback((id: string) => {
    setReauthId(id);
    setShowAdd(true);
  }, [setShowAdd]);

  const closeAddModal = useCallback(() => {
    setShowAdd(false);
    setReauthId(null);
  }, [setShowAdd]);

  const handleAccountAdded = useCallback((completion: CodexAccountMutationCompletion) => {
    void controller.syncAfterAccountAdded();
    showActionFeedback(
      t(completion.validationPending
        ? "pws.healthLabel.validationPending"
        : completion.catalogRefreshPending
        ? "codexAuth.catalogRefreshPending"
        : "codexAuth.accountAdded"),
      completion.validationPending || completion.catalogRefreshPending ? "warn" : "ok",
    );
    closeAddModal();
    setModelsNotice(completion.validationPending ? null : { catalogRefreshPending: completion.catalogRefreshPending });
  }, [closeAddModal, controller, showActionFeedback, t, setModelsNotice]);

  const editAlias = async (account: CodexAccountEntry) => {
    const entered = await requestTextValue({
      message: t("prov.aliasPrompt"),
      initialValue: account.alias ?? "",
      maxLength: CREDENTIAL_ALIAS_MAX_LENGTH,
      validate: value => credentialAliasRejection(value, t),
    });
    if (entered === null) return;
    const result = await controller.saveAlias(account.id, entered);
    showActionFeedback(t(result.ok ? "prov.aliasSaved" : "prov.aliasSaveFailed"), result.ok ? "ok" : "err");
  };

  const togglePaused = async (account: CodexAccountEntry) => {
    const paused = !account.paused;
    const result = await controller.setAccountPaused(account.id, paused);
    if (!result.ok && result.reason === "busy") return;
    showActionFeedback(t(result.ok
      ? paused ? "codexAuth.pauseSucceeded" : "codexAuth.resumeSucceeded"
      : paused ? "codexAuth.pauseFailed" : "codexAuth.resumeFailed", {
      email: account.alias ?? account.email,
    }), result.ok ? "ok" : "err");
  };

  const toggleAllCreditsAfterLimit = async (enabled: boolean) => {
    const selected = accounts.find(account => activeId ? account.id === activeId : account.isMain);
    if (!selected) return;
    const result = await controller.setAccountCreditsAfterLimit(selected.id, enabled);
    if (!result.ok && result.reason === "busy") return;
    showActionFeedback(t(result.ok
      ? enabled ? "codexAuth.creditsAllOnSucceeded" : "codexAuth.creditsAllOffSucceeded"
      : "codexAuth.creditsAllUpdateFailed"), result.ok ? "ok" : "err");
  };

  const toggleCreditsAfterLimit = async (account: CodexAccountEntry, enabled: boolean) => {
    const result = await controller.setAccountCreditsAfterLimit(account.id, enabled);
    if (!result.ok && result.reason === "busy") return;
    showActionFeedback(t(result.ok
      ? enabled ? "codexAuth.creditsOnSucceeded" : "codexAuth.creditsOffSucceeded"
      : "codexAuth.creditsUpdateFailed", {
      email: account.alias ?? account.email,
    }), result.ok ? "ok" : "err");
  };

  const remove = async (id: string) => {
    const label = accounts.find(account => account.id === id)?.email ?? t("pws.accountOrdinal", { count: "1" });
    if (!(await confirmAction({ message: t("codexAuth.removeConfirm", { id: label }), confirmLabel: t("common.remove"), tone: "danger" }))) return;
    const result = await controller.removeAccount(id);
    if (!result.ok) {
      showActionFeedback(t("codexAuth.removeFailed"), "err");
    } else if (result.catalogRefreshPending) {
      showActionFeedback(t("codexAuth.catalogRefreshPending"), "warn");
    }
  };

  const refreshQuotas = async () => {
    setRefreshingQuota(true);
    try {
      const ok = await load(true, { validatePending: true });
      showActionFeedback(t(ok ? "codexAuth.quotaRefreshed" : "codexAuth.quotaRefreshFailed"), ok ? "ok" : "err");
    } finally {
      setRefreshingQuota(false);
    }
  };

  const toggleQuotaAutoRefresh = async (enabled: boolean) => {
    const scope = quotaScopeRef.current;
    if (quotaMutationRef.current || !scope || scope.signal.aborted || quotaAutoRefreshSettings === null || loadState !== "ready") return;
    const pending = createBoundedFetch(30_000);
    quotaMutationRef.current = pending;
    quotaAutoRefreshMutationRevisionRef.current += 1;
    const current = () => quotaScopeRef.current === scope && !scope.signal.aborted;
    const windows = quotaActivationWindows(accounts, quotaAutoRefreshSettings);
    setQuotaBusyScope(apiBase);
    setQuotaFeedback(null);
    let failed = false;
    try {
      for (const target of windows) {
        // Missing quota can be transient. ON must not revoke an existing opt-in;
        // only an explicit OFF action clears flags for unavailable windows.
        if (enabled && !target.available) continue;
        const requested = enabled;
        if (target.enabled === requested) continue;
        if (!current()) return;
        if (pending.signal.aborted) { failed = true; break; }
        try {
          // Ordered field-patches to shared settings; stop unsent writes on proxy
          // changes. Parallel dispatch would spend the rest of the batch before
          // cancellation can take effect (covered by the deferred-write test).
          // react-doctor-disable-next-line react-doctor/async-await-in-loop -- intentional sequential settings mutations
          const response = await fetch(`${apiBase}/api/settings`, {
            method: "PUT", headers: { "content-type": "application/json" }, signal: pending.signal,
            body: JSON.stringify({ codexQuotaAutoRefresh: { id: target.id, window: target.window, enabled: requested } }),
          });
          if (!response.ok) throw new Error("save");
        } catch { failed = true; }
      }
      if (!current()) return;
      // Granular writes can partially commit (including a lost response). Read back
      // the authoritative map; never claim that a failed batch rolled back.
      pending.clear();
      const read = createBoundedFetch(15_000);
      quotaMutationRef.current = read;
      try {
        const response = await fetch(`${apiBase}/api/settings`, { signal: read.signal });
        if (!response.ok) throw new Error("read");
        const saved = readQuotaActivationSettings(await response.json());
        if (!current()) return;
        failed ||= windows.some(target => (!enabled || target.available)
          && (saved[target.id]?.[target.window] === true) !== enabled);
        setQuotaState({ apiBase, revision: quotaReadRevision, settings: saved, error: false });
      } catch {
        if (!current()) return;
        failed = true;
        setQuotaState({ apiBase, revision: quotaReadRevision, settings: null, error: true });
      } finally { read.clear(); }
      if (!current()) return;
      failedQuotaTarget.current = failed ? { apiBase, enabled } : null;
      setQuotaFeedback({ apiBase, message: t(failed ? "codexAuth.quotaAutoRefreshPartial" : "codexAuth.quotaAutoRefreshUpdated"), failed });
    } finally {
      pending.clear();
      if (current()) {
        quotaMutationRef.current = null;
        setQuotaBusyScope(null);
      }
    }
  };

  useEffect(() => {
    // The credits hook shares the parsed settings GET above; this scope still
    // owns teardown for the quota activation writes and their reconciliation.
    const abort = new AbortController();
    quotaScopeRef.current = abort;
    return () => {
      abort.abort();
      quotaMutationRef.current?.controller.abort();
      quotaMutationRef.current?.clear();
      quotaMutationRef.current = null;
    };
  }, [apiBase, quotaReadRevision]);


  const openResetPopup = async (account: CodexAccountEntry) => {
    setResetPopup(account);
    setResetConfirm(false);
    setCreditDetails(null);
    setCreditDetailsLoading(true);
    try {
      const resp = await fetch(`${apiBase}/api/codex-auth/reset-credits?accountId=${encodeURIComponent(account.id)}`);
      const data = await readJsonIfOk<{ credits?: { granted_at: string; expires_at: string }[] }>(resp);
      if (data) {
        const sorted = (data.credits ?? []).sort((a, b) =>
          new Date(a.granted_at).getTime() - new Date(b.granted_at).getTime()
        );
        setCreditDetails(sorted);
      }
    } catch { /* detail fetch is non-blocking */ }
    finally { setCreditDetailsLoading(false); }
  };

  const handleRedeem = async (accountId: string) => {
    setRedeeming(true);
    try {
      const result = await redeemResetCredit(apiBase, accountId, t, load);
      if (result.close) {
        setResetPopup(null);
        setResetConfirm(false);
      }
      if (result.toast) {
        showActionFeedback(result.toast, result.ok ? "ok" : "err");
      }
    } finally {
      setRedeeming(false);
    }
  };

  const main = accounts.find(a => a.isMain);
  const pool = accounts.filter(a => !a.isMain && a.id === activeId);
  // The rows the global credit switch covers: the main login once it has a credential, then the pool.
  const creditRows = [...(!activeId && main?.hasCredential ? [main] : []), ...pool];
  const creditSummary = creditSpendSummary(creditRows);
  const mainSelected = !activeId || activeId === "__main__";
  const isMainActive = mainSelected && !main?.paused;
  const pauseBusy = pauseUpdatingId !== null;
  // The standalone Codex Auth page keeps the doctor-copy affordance; the embedded
  // Providers workspace account surface does not.
  const showDoctorCopy = !embedded;

  return (
    <div className={`acc-v2 flex flex-col ${embedded ? "gap-3" : "gap-4"}`}>
      <CodexAccountPoolPageHead
        t={t}
        embedded={embedded}
        creditsVisible={credits.visible}
        creditsBusy={credits.busy}
        onToggleCredits={() => { void credits.toggle(); }}
        creditSpendControl={loadState === "loading" && accounts.length === 0 ? undefined : (
          <CodexCreditSpendSwitch
            summary={creditSummary}
            busy={controller.creditsAfterLimitUpdatingId !== null}
            onToggleAll={enabled => { void toggleAllCreditsAfterLimit(enabled); }}
          />
        )}
        refreshingQuota={refreshingQuota}
        actionFeedback={actionFeedback}
        actionFeedbackTone={actionFeedbackTone}
        pauseBusy={pauseBusy}
        onRefresh={() => { void refreshQuotas(); }}
      />

      {banner}


      {!embedded && (
        <CodexAccountPoolActions
          t={t}
          refreshingQuota={refreshingQuota}
          pauseBusy={pauseBusy}
          onRefresh={() => { void refreshQuotas(); }}
        />
      )}

      {/* Skeleton must sit where main/pool cards will be — never above the account-mode
          banner, or the strip collapses on ready and shoves the whole page up (CLS). */}
      <CodexAccountPoolLoadStates
        t={t}
        loadState={loadState}
        refreshFailed={refreshFailed}
        accountsCount={accounts.length}
        onRetry={() => { void load(); }}
      />

      {!(loadState === "loading" && accounts.length === 0) && (
        <>
          {mainSelected && main?.hasCredential && <Panel className="acc-accounts acc-accounts--main">
            <CodexAccountPoolMainCard
              t={t}
              main={main}
              creditsVisible={credits.visible}
              loading={loadState === "loading"}
              isMainActive={isMainActive}
              onTogglePause={togglePaused}
              pauseUpdatingId={pauseUpdatingId}
              pauseBusy={pauseBusy}
              onToggleCreditsAfterLimit={(entry, enabled) => { void toggleCreditsAfterLimit(entry, enabled); }}
              creditsAfterLimitUpdatingId={controller.creditsAfterLimitUpdatingId}
              onOpenReset={openResetPopup}
              onCopyDoctor={showDoctorCopy ? copyDoctor : undefined}
              doctorCopyOutcomeFor={showDoctorCopy ? doctorCopy.outcomeFor : undefined}
              onManageMainHardLock={hasMainHardLockSetting ? manageMainHardLock : undefined}
              mainReauth={mainReauth}
            />
          </Panel>}

          {(pool.length > 0 || !main?.hasCredential) && <Panel
            className="acc-accounts"
            title={t("pws.availableAccounts")}
            meta={pool.length > 0 ? String(pool.length) : undefined}
            actions={!main?.hasCredential && pool.length === 0 ? <CodexAccountPoolAddButton t={t} onAdd={() => setShowAdd(true)} /> : undefined}
          >
            {activePoolNeedsReauth && activePoolAccount && (
              <CodexAccountPoolReauthBanner onReauth={() => openReauth(activePoolAccount.id)} />
            )}

            {!main?.hasCredential && pool.length === 0 && (
              <div className="acc-pool-empty">
                <p className="acc-pool-empty__title">{t("pws.notLoggedInTitle")}</p>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowAdd(true)}>
                  <IconPlus width={14} /> {t("prov.login")}
                </button>
              </div>
            )}

            <CodexAccountPoolCards
              pool={pool}
              creditsVisible={credits.visible}
              loading={loadState === "loading"}
              activeId={activeId}
              onOpenReset={openResetPopup}
              onTogglePause={togglePaused}
              pauseUpdatingId={pauseUpdatingId}
              pauseBusy={pauseBusy}
              onToggleCreditsAfterLimit={(entry, enabled) => { void toggleCreditsAfterLimit(entry, enabled); }}
              creditsAfterLimitUpdatingId={controller.creditsAfterLimitUpdatingId}
              onReauth={openReauth}
              onEditAlias={editAlias}
              onRemove={remove}
              onCopyDoctor={showDoctorCopy ? copyDoctor : undefined}
              doctorCopyOutcomeFor={showDoctorCopy ? doctorCopy.outcomeFor : undefined}
            />
          </Panel>}
        </>
      )}

      <CodexAuthAdvancedSettings
        t={t}
        open={advancedOpen}
        onToggle={() => setAdvancedOpen(open => !open)}
      >
        <CodexQuotaAutoRefreshSetting
          windows={quotaActivationWindows(accounts, quotaAutoRefreshSettings ?? {})}
          ready={quotaAutoRefreshSettings !== null && loadState === "ready"}
          busy={quotaAutoRefreshBusy}
          loadError={quotaLoadError || loadState === "error"}
          feedback={quotaFeedback?.apiBase === apiBase ? quotaFeedback : null}
          onToggle={enabled => { void toggleQuotaAutoRefresh(enabled); }}
          onRetry={() => {
            if (quotaLoadError || quotaAutoRefreshSettings === null || loadState !== "ready") {
              setQuotaReadRevision(value => value + 1);
              void load();
            } else if (failedQuotaTarget.current?.apiBase === apiBase) {
              void toggleQuotaAutoRefresh(failedQuotaTarget.current.enabled);
            }
          }}
        />

        {advancedExtras}
      </CodexAuthAdvancedSettings>

      {resetPopup && (
        <CodexAccountResetModal
          resetPopup={resetPopup}
          resetConfirm={resetConfirm}
          creditDetails={creditDetails}
          creditDetailsLoading={creditDetailsLoading}
          redeeming={redeeming}
          onClose={() => { setResetPopup(null); setResetConfirm(false); setCreditDetails(null); }}
          onShowConfirm={() => setResetConfirm(true)}
          onCancelConfirm={() => setResetConfirm(false)}
          onRedeem={() => { void handleRedeem(resetPopup.id); }}
        />
      )}

      {showAdd && (
        <AddCodexAccountModal
          apiBase={apiBase}
          reauthAccountId={reauthId ?? undefined}
          onClose={closeAddModal}
          onAdded={handleAccountAdded}
        />
      )}
      {modelsNotice && <ProviderModelsNotice
        provider="openai" loading={false} failed={false} providerKnown initialRegistration={false}
        catalogRefreshPending={modelsNotice.catalogRefreshPending}
        onClose={() => setModelsNotice(null)}
        onOpenModels={() => { setModelsNotice(null); navigateHash("models"); }}
      />}
    </div>
  );
}
