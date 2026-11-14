import { useEffect, useId, useRef, useState } from "react";
import { confirmAction } from "../action-dialogs";
import { createBoundedFetch, type BoundedFetch } from "../bounded-fetch";
import { readJsonOrThrow } from "../fetch-json";
import { liteTranslator } from "../i18n/lite-copy";
import { useI18n, type TKey } from "../i18n/shared";
import { Notice, Select } from "../ui";
import {
  CUSTOM_OPTION,
  REASONING_EFFORT_LEVELS,
  modelDisplayNameValidationKey,
  type ModelRow,
} from "../pages/models-shared";
import {
  buildDraftCost,
  isRecord,
  loadDraft,
  RATE_FIELDS,
  sameModelCost,
  type InvalidField,
} from "./model-price-cost";
import ModelPriceFields from "./ModelPriceFields";
import { MetadataSource, ModelCostSummary, ModelReasoningSummary } from "./model-metadata";

const MODALITIES = ["text", "image", "audio"] as const;
const CONTEXT_PRESETS = ["100000", "128000", "200000", "256000", "352000", "500000", "1000000"];
const REQUEST_TIMEOUT_MS = 60_000;
type Phase = "ready" | "saving" | "restoring" | "unknown" | "saved-stale";
type Axis =
  | "displayName"
  | "contextWindow"
  | "inputModalities"
  | "reasoningEfforts"
  | "defaultReasoningEffort"
  | "cost";

function sortedKey(values: readonly string[]): string {
  return values.toSorted().join(",");
}

/** undefined when the draft is not a usable window, null when the operator cleared it. */
function parseContextWindow(raw: string): number | null | undefined {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const value = Number(trimmed.replace(/[_,\s]/g, ""));
  return Number.isSafeInteger(value) && value > 0 ? value : undefined;
}

interface ModelSettingsDialogProps {
  row: ModelRow;
  apiBase: string;
  onRefresh: (signal: AbortSignal) => Promise<boolean>;
  onFeedback: (ok: boolean, message: string) => void;
  onClose: () => void;
}

export default function ModelSettingsDialog({
  row,
  apiBase,
  onRefresh,
  onFeedback,
  onClose,
}: ModelSettingsDialogProps) {
  const { t, locale } = useI18n();
  const lite = liteTranslator(locale);
  const id = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const reloadRef = useRef<HTMLButtonElement>(null);
  const requestRef = useRef<BoundedFetch | null>(null);
  const priceFieldRefs = useRef<Partial<Record<InvalidField, HTMLInputElement>>>({});
  const [baseline] = useState(() => ({
    name: row.displayName ?? row.namespaced,
    contextWindow:
      (row.metadataOverrides?.contextWindow ?? row.contextWindow) === undefined
        ? ""
        : String(row.metadataOverrides?.contextWindow ?? row.contextWindow),
    modalities: row.inputModalities ?? [],
    ladder: row.reasoningEfforts ?? [],
    defaultEffort: row.defaultReasoningEffort ?? "",
    price: loadDraft(row.costOverride),
  }));
  const [phase, setPhase] = useState<Phase>("ready");
  const [modelId, setModelId] = useState(row.id);
  const [name, setName] = useState(baseline.name);
  const [contextDraft, setContextDraft] = useState(baseline.contextWindow);
  const [customContext, setCustomContext] = useState(
    baseline.contextWindow !== "" && !CONTEXT_PRESETS.includes(baseline.contextWindow),
  );
  const [modalities, setModalities] = useState(baseline.modalities);
  const [reasoning, setReasoning] = useState(row.reasoningEfforts !== undefined);
  const [ladder, setLadder] = useState(baseline.ladder);
  const [defaultEffort, setDefaultEffort] = useState(baseline.defaultEffort);
  const [priceRates, setPriceRates] = useState(baseline.price.rates);
  const [priceMode, setPriceMode] = useState(baseline.price.mode);
  const [customPrice, setCustomPrice] = useState(baseline.price.custom);
  const [priceOverride, setPriceOverride] = useState(
    !row.subscription && row.costOverride !== undefined,
  );
  const [invalidPrice, setInvalidPrice] = useState<InvalidField | null>(null);
  const [priceEdited, setPriceEdited] = useState(false);
  const [resetAxes, setResetAxes] = useState<Axis[]>([]);
  const [errorKey, setErrorKey] = useState<TKey | null>(null);
  const [idError, setIdError] = useState(false);
  const busy = phase === "saving" || phase === "restoring";
  const terminal = phase === "unknown" || phase === "saved-stale";
  const locked = busy || terminal;
  const inherited = row.inheritedMetadata;
  const overrides = row.metadataOverrides;
  const hasOverride = (axis: Axis) =>
    overrides?.[axis] !== undefined ||
    (axis === "displayName"
      ? row.displayNameOverride !== undefined
      : axis === "contextWindow"
        ? row.contextWindowDeclared !== undefined
        : axis === "inputModalities"
          ? row.inputModalitiesDeclared !== undefined
          : axis === "cost"
            ? row.costOverride !== undefined
            : false);

  useEffect(() => {
    const dialog = dialogRef.current;
    const opener = document.activeElement as HTMLElement | null;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      requestRef.current?.controller.abort();
      requestRef.current?.clear();
      requestRef.current = null;
      if (dialog?.open) dialog.close();
      if (opener?.isConnected) opener.focus();
    };
  }, []);

  useEffect(() => {
    if (terminal) reloadRef.current?.focus();
  }, [terminal]);

  const editAxis = (axis: Axis) => {
    setResetAxes((current) => current.filter((field) => field !== axis));
    setErrorKey(null);
  };

  const resetAxis = (axis: Axis) => {
    setResetAxes((current) => [...new Set([...current, axis])]);
    setErrorKey(null);
    switch (axis) {
      case "displayName":
        setName(inherited?.displayName ?? row.namespaced);
        break;
      case "contextWindow": {
        const raw = inherited?.contextWindow;
        const next =
          row.contextCap === undefined
            ? raw === undefined
              ? ""
              : String(raw)
            : String(raw === undefined ? row.contextCap : Math.min(raw, row.contextCap));
        setContextDraft(next);
        setCustomContext(next !== "" && !CONTEXT_PRESETS.includes(next));
        break;
      }
      case "inputModalities":
        setModalities(inherited?.inputModalities ?? []);
        break;
      case "reasoningEfforts":
        setReasoning(inherited?.reasoningEfforts !== undefined);
        setLadder(inherited?.reasoningEfforts ?? []);
        if (defaultEffort && !inherited?.reasoningEfforts?.includes(defaultEffort)) {
          setDefaultEffort(inherited?.defaultReasoningEffort ?? "");
          setResetAxes((current) => [...new Set<Axis>([...current, "defaultReasoningEffort"])]);
        }
        break;
      case "defaultReasoningEffort":
        setDefaultEffort(inherited?.defaultReasoningEffort ?? "");
        break;
      case "cost":
        setPriceOverride(false);
        setInvalidPrice(null);
        break;
    }
  };

  const resetControl = (axis: Axis, label = t("models.settingsRestore")) => (
    <div className="row models-field-row">
      <MetadataSource
        linked
        value={row.metadataSources?.[axis === "cost" ? "effectiveCost" : axis]}
        reset={resetAxes.includes(axis)}
      />
      {hasOverride(axis) && !resetAxes.includes(axis) && (
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => resetAxis(axis)}
          disabled={locked}
        >
          {label}
        </button>
      )}
    </div>
  );

  const togglePriceOverride = (next: boolean) => {
    setPriceOverride(next);
    if (!next) {
      resetAxis("cost");
      return;
    }
    editAxis("cost");
    const automatic =
      inherited?.effectiveCost ?? (row.costOverride ? undefined : row.effectiveCost);
    const nextDraft = row.costOverride
      ? baseline.price
      : {
          ...loadDraft(undefined),
          rates: Object.fromEntries(
            RATE_FIELDS.map((field) => [field, automatic?.[field]?.toString() ?? ""]),
          ) as typeof priceRates,
        };
    setPriceRates(nextDraft.rates);
    setPriceMode(nextDraft.mode);
    setCustomPrice(nextDraft.custom);
  };
  const reload = async () => {
    if (requestRef.current) return;
    const bounded = createBoundedFetch(REQUEST_TIMEOUT_MS);
    requestRef.current = bounded;
    try {
      if (!(await onRefresh(bounded.signal))) return;
      bounded.signal.throwIfAborted();
      if (requestRef.current !== bounded) return;
      onFeedback(true, t("models.settingsReloaded", { model: row.namespaced }));
      onClose();
    } catch {
      // Keep the terminal state: the operator still needs a successful read before another write.
    } finally {
      bounded.clear();
      if (requestRef.current === bounded) requestRef.current = null;
    }
  };

  const toggleReasoning = (next: boolean) => {
    editAxis("reasoningEfforts");
    setReasoning(next);
    if (!next) return;
    // First enable seeds from what the model advertises rather than the full shared ladder: a
    // provider that accepts only a subset would otherwise be handed levels its API rejects.
    if (ladder.length > 0) return;
    const advertised = Array.isArray(row.reasoningEfforts) ? row.reasoningEfforts : undefined;
    setLadder(advertised && advertised.length > 0 ? [...advertised] : [...REASONING_EFFORT_LEVELS]);
  };

  const toggleLevel = (level: string, on: boolean) => {
    editAxis("reasoningEfforts");
    setLadder((prev) => (on ? [...prev, level] : prev.filter((value) => value !== level)));
    // The default has to stay inside the ladder, so dropping its level drops it too.
    if (!on && defaultEffort === level) {
      editAxis("defaultReasoningEffort");
      setDefaultEffort("");
    }
  };

  const requestClose = () => {
    if (!busy) onClose();
  };

  const send = async (patch: Record<string, unknown>, restoring: boolean) => {
    if (requestRef.current) return;
    const bounded = createBoundedFetch(REQUEST_TIMEOUT_MS);
    requestRef.current = bounded;
    setPhase(restoring ? "restoring" : "saving");
    setErrorKey(null);
    let confirmed = false;
    let rejected = false;
    try {
      const response = await fetch(apiBase + "/api/model-settings", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ provider: row.provider, modelId: row.id, ...patch }),
        signal: bounded.signal,
      });
      // A 4xx is answered before anything is written, so the outcome is known: nothing saved.
      // The form stays editable, and the server's untranslated text is not shown.
      if (response.status >= 400 && response.status < 500) {
        rejected = true;
        throw new Error("model settings rejected");
      }
      const result = await readJsonOrThrow<unknown>(response);
      bounded.signal.throwIfAborted();
      // Validate the receipt against the request: a 200 for another row would otherwise be
      // reported as this row's save, and the reload would appear to disagree with the toast.
      if (
        !isRecord(result) ||
        result.ok !== true ||
        result.provider !== row.provider ||
        result.modelId !== row.id ||
        typeof result.changed !== "boolean" ||
        typeof result.hasOverrides !== "boolean" ||
        typeof result.saved !== "boolean" ||
        result.saved !== result.changed ||
        !isRecord(result.catalogRefresh) ||
        !["committed", "skipped", "failed"].includes(String(result.catalogRefresh.status))
      ) {
        throw new Error("invalid model-settings receipt");
      }
      if (requestRef.current !== bounded) return;
      confirmed = true;
      if (!result.changed) {
        onFeedback(
          true,
          t(
            restoring && !result.hasOverrides
              ? "models.settingsNothingToRestore"
              : "models.settingsNoChange",
            { model: row.namespaced },
          ),
        );
        onClose();
        return;
      }
      // `catalogRefresh` describes the Codex app's catalog, not this list: the dashboard reads the
      // saved config directly. A skip the operator cannot act on (no managed catalog) is normal;
      // only a failed or retryable refresh is worth a warning, and only after the list reloads.
      const refresh = result.catalogRefresh;
      const codexStale =
        refresh.status === "failed" || (refresh.status === "skipped" && refresh.retryable === true);
      if (!(await onRefresh(bounded.signal))) throw new Error("catalog refresh failed");
      bounded.signal.throwIfAborted();
      if (requestRef.current !== bounded) return;
      if (codexStale) {
        onFeedback(false, t("models.settingsSavedCodexStale", { model: row.namespaced }));
      } else if (restoring) {
        onFeedback(true, t("models.settingsRestored", { model: row.namespaced }));
      } else {
        onFeedback(true, t("models.settingsSaved", { model: row.namespaced }));
      }
      onClose();
    } catch {
      if (requestRef.current !== bounded) return;
      if (rejected) {
        setPhase("ready");
        setErrorKey("models.settingsRejected");
        return;
      }
      setPhase(confirmed ? "saved-stale" : "unknown");
      setErrorKey(confirmed ? "models.settingsRefreshFailed" : "models.settingsSaveFailed");
    } finally {
      bounded.clear();
      if (requestRef.current === bounded) requestRef.current = null;
    }
  };

  const submit = () => {
    if (phase !== "ready") return;
    const patch: Record<string, unknown> = Object.fromEntries(
      resetAxes.map((axis) => [axis, null]),
    );
    const nextId = modelId.trim();
    if (row.custom && !nextId) {
      setIdError(true);
      return;
    }
    if (row.custom && nextId !== row.id) patch.newModelId = nextId;
    if (!resetAxes.includes("displayName") && name !== baseline.name) {
      if (row.custom && !name.trim()) {
        patch.displayName = null;
      } else {
        const validation = modelDisplayNameValidationKey(name);
        if (validation) {
          setErrorKey(validation);
          return;
        }
        patch.displayName = name.trim();
      }
    }
    const parsedWindow = parseContextWindow(contextDraft);
    if (parsedWindow === undefined) {
      setErrorKey("models.contextInvalid");
      return;
    }
    if (
      !resetAxes.includes("contextWindow") &&
      parsedWindow !== parseContextWindow(baseline.contextWindow)
    ) {
      patch.contextWindow = parsedWindow;
    }
    if (
      !resetAxes.includes("inputModalities") &&
      sortedKey(modalities) !== sortedKey(baseline.modalities)
    ) {
      patch.inputModalities = modalities.length > 0 ? modalities : null;
    }
    if (
      !resetAxes.includes("reasoningEfforts") &&
      (reasoning !== (row.reasoningEfforts !== undefined) ||
        sortedKey(ladder) !== sortedKey(baseline.ladder))
    ) {
      patch.reasoningEfforts = reasoning ? ladder : null;
    }
    if (!resetAxes.includes("defaultReasoningEffort") && defaultEffort !== baseline.defaultEffort) {
      patch.defaultReasoningEffort = defaultEffort || null;
    }
    if (priceOverride && (priceEdited || !row.costOverride)) {
      const badField = (
        Object.entries(priceFieldRefs.current) as [InvalidField, HTMLInputElement][]
      ).find(([, input]) => input.validity.badInput)?.[0];
      const outcome = buildDraftCost(priceRates, priceMode, customPrice, badField);
      if ("field" in outcome) {
        setInvalidPrice(outcome.field);
        setErrorKey(
          outcome.field === "threshold" || outcome.field.startsWith("custom")
            ? "pricing.override.customInvalid"
            : "pricing.override.invalid",
        );
        priceFieldRefs.current[outcome.field]?.focus();
        return;
      }
      if (!row.costOverride || !sameModelCost(row.costOverride, outcome.cost))
        patch.cost = outcome.cost;
    } else if (!row.costOverride) {
      delete patch.cost;
    }
    // Effective values are read from the same row as hover. Only intentional changes are stored.
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }
    void send(patch, false);
  };

  const restore = async () => {
    if (phase !== "ready") return;
    const confirmed = await confirmAction({
      message: lite("modelRestoreAllConfirm", { model: row.namespaced }),
      confirmLabel: lite("modelRestoreAll"),
      tone: "danger",
    });
    if (!confirmed) return;
    void send(
      {
        displayName: null,
        cost: null,
        contextWindow: null,
        inputModalities: null,
        reasoningEfforts: null,
        defaultReasoningEffort: null,
      },
      true,
    );
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal-overlay"
      aria-labelledby={id + "-title"}
      onCancel={(event) => {
        event.preventDefault();
        requestClose();
      }}
    >
      <button
        type="button"
        className="modal-backdrop-dismiss"
        aria-label={t("common.close")}
        tabIndex={-1}
        disabled={busy}
        onClick={requestClose}
      />
      <form
        className="modal-card model-display-name-dialog model-price-dialog"
        role="document"
        noValidate
        aria-busy={busy}
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div className="modal-head">
          <h3 id={id + "-title"}>{t("models.settingsTitle", { model: row.namespaced })}</h3>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={requestClose}
          >
            {t("common.close")}
          </button>
        </div>

        {errorKey && <Notice tone={phase === "saved-stale" ? "warn" : "err"}>{t(errorKey)}</Notice>}

        <div className="models-field-stack model-price-body">
          <div className="text-label models-field">
            {t("models.customFieldModelId")}
            {row.custom ? (
              <input
                className="input"
                aria-label={t("models.customFieldModelId")}
                value={modelId}
                disabled={locked}
                aria-invalid={idError || undefined}
                onChange={(event) => {
                  setModelId(event.target.value);
                  setIdError(false);
                }}
              />
            ) : (
              <code className="mono text-control">{row.namespaced}</code>
            )}
            {idError && <Notice tone="err">{lite("modelIdRequired")}</Notice>}
          </div>

          <div className="models-field">
            <label className="field-label" htmlFor={id + "-name"}>
              {t("models.displayNameField")}
            </label>
            <input
              className="input"
              id={id + "-name"}
              value={name}
              maxLength={129}
              disabled={locked}
              onChange={(event) => {
                editAxis("displayName");
                setName(event.target.value);
              }}
            />
            {resetControl("displayName", t("models.displayNameReset"))}
          </div>

          <section className="models-field" aria-labelledby={id + "-pricing"}>
            <h4 id={id + "-pricing"} className="field-label">
              {lite("modelPricing")}
            </h4>
            <ModelCostSummary
              cost={resetAxes.includes("cost") ? inherited?.effectiveCost : row.effectiveCost}
              schedule={row.pricingSchedule}
              subscription={row.subscription}
              linked
            />
            {row.subscription &&
              row.costOverride &&
              resetControl("cost", t("pricing.override.reset"))}
            {!row.subscription && (
              <>
                {resetControl("cost", t("pricing.override.reset"))}
                <label className="row models-modality-option">
                  <input
                    type="checkbox"
                    checked={priceOverride}
                    disabled={locked}
                    onChange={(event) => togglePriceOverride(event.target.checked)}
                  />
                  <span className="text-control">{lite("modelOverridePricing")}</span>
                </label>
                {priceOverride && (
                  <ModelPriceFields
                    draft={priceRates}
                    setDraft={setPriceRates}
                    pricingMode={priceMode}
                    setPricingMode={setPriceMode}
                    customDraft={customPrice}
                    setCustomDraft={setCustomPrice}
                    locked={locked}
                    invalidField={invalidPrice}
                    customFieldRefs={priceFieldRefs}
                    onEdit={() => {
                      editAxis("cost");
                      setInvalidPrice(null);
                      setPriceEdited(true);
                    }}
                  />
                )}
              </>
            )}
          </section>

          <div className="text-label models-field">
            {t("models.customFieldContext")}
            {row.contextCap !== undefined && (
              <span className="muted text-caption">
                {lite("modelContextEffective", {
                  value: row.contextWindow?.toLocaleString() ?? lite("modelUnknown"),
                  cap: row.contextCap.toLocaleString(),
                })}
              </span>
            )}
            <div className="row models-field-row">
              <Select
                value={customContext ? CUSTOM_OPTION : contextDraft}
                options={[
                  { value: "", label: "—" },
                  { value: "100000", label: "100k" },
                  { value: "128000", label: "128k" },
                  { value: "200000", label: "200k" },
                  { value: "256000", label: "256k" },
                  { value: "352000", label: "352k" },
                  { value: "500000", label: "500k" },
                  { value: "1000000", label: "1M" },
                  { value: CUSTOM_OPTION, label: t("models.custom") },
                ]}
                onChange={(value) => {
                  if (value === CUSTOM_OPTION) {
                    setCustomContext(true);
                    return;
                  }
                  setCustomContext(false);
                  editAxis("contextWindow");
                  setContextDraft(value);
                }}
                disabled={locked}
                label={t("models.customFieldContext")}
                // A portaled menu lands under the modal's top layer, where the backdrop takes its clicks.
                portal={false}
              />
              {customContext && (
                <input
                  className="input"
                  style={{ width: 120 }}
                  inputMode="numeric"
                  value={contextDraft}
                  onChange={(event) => {
                    editAxis("contextWindow");
                    setContextDraft(event.target.value);
                  }}
                  disabled={locked}
                  placeholder={t("models.customPlaceholder")}
                  aria-label={t("models.customFieldContext")}
                />
              )}
            </div>
            {resetControl("contextWindow")}
          </div>

          <div className="text-label models-field">
            {t("models.customFieldModalities")}
            <div className="row models-field-row">
              {[...new Set([...MODALITIES, ...modalities])].map((modality) => (
                <label key={modality} className="row models-modality-option">
                  <input
                    type="checkbox"
                    checked={modalities.includes(modality)}
                    onChange={(event) => {
                      const checked = event.target.checked;
                      editAxis("inputModalities");
                      setModalities((prev) =>
                        checked ? [...prev, modality] : prev.filter((value) => value !== modality),
                      );
                    }}
                    disabled={locked || !(MODALITIES as readonly string[]).includes(modality)}
                  />
                  <span className="text-control">{modality}</span>
                </label>
              ))}
            </div>
            {modalities.length === 0 && (
              <span className="muted text-caption">{lite("modelUnknown")}</span>
            )}
            {resetControl("inputModalities")}
          </div>
          <div className="models-field">
            <span className="text-label">{lite("modelOutputModalities")}</span>
            <span className="text-control">
              {row.outputModalities?.join(", ") || lite("modelUnknown")}
            </span>
            <MetadataSource linked value={row.metadataSources?.outputModalities} />
          </div>
          <div className="models-field">
            <span className="text-label">{lite("modelInputLimit")}</span>
            <span className="text-control">
              {row.maxInputTokens?.toLocaleString() ?? lite("modelUnknown")}
            </span>
            <MetadataSource linked value={row.metadataSources?.maxInputTokens} />
          </div>
          <div className="models-field">
            <span className="text-label">{lite("modelOutputLimit")}</span>
            <span className="text-control">
              {row.maxOutputTokens?.toLocaleString() ?? lite("modelUnknown")}
            </span>
            <MetadataSource linked value={row.metadataSources?.maxOutputTokens} />
          </div>

          <div className="text-label models-field">
            {t("models.customFieldReasoning")}
            <span className="text-control">
              <ModelReasoningSummary row={row} />
            </span>
            <div className="row models-field-row">
              <label className="row models-modality-option">
                <input
                  type="checkbox"
                  checked={reasoning}
                  onChange={(event) => toggleReasoning(event.target.checked)}
                  disabled={locked}
                />
                <span className="text-control">{lite("modelReasoningLevels")}</span>
              </label>
            </div>
            {reasoning && (
              <div className="row models-field-row" style={{ flexWrap: "wrap" }}>
                {REASONING_EFFORT_LEVELS.map((level) => (
                  <label key={level} className="row models-modality-option">
                    <input
                      type="checkbox"
                      checked={ladder.includes(level)}
                      onChange={(event) => toggleLevel(level, event.target.checked)}
                      disabled={locked}
                    />
                    <span className="text-control">
                      {t(("models.reasoningEffort." + level) as TKey)}
                    </span>
                  </label>
                ))}
              </div>
            )}
            {resetControl("reasoningEfforts")}
          </div>

          {(reasoning || defaultEffort || hasOverride("defaultReasoningEffort")) && (
            <div className="text-label models-field">
              {t("models.settingsDefaultEffort")}
              <div className="row models-field-row">
                <Select
                  value={defaultEffort}
                  options={[
                    { value: "", label: "—" },
                    ...[...new Set([...ladder, ...(defaultEffort ? [defaultEffort] : [])])].map(
                      (level) => ({
                        value: level,
                        label: t(("models.reasoningEffort." + level) as TKey),
                      }),
                    ),
                  ]}
                  onChange={(value) => {
                    editAxis("defaultReasoningEffort");
                    setDefaultEffort(value);
                  }}
                  disabled={locked}
                  label={t("models.settingsDefaultEffort")}
                  portal={false}
                />
              </div>
              {resetControl("defaultReasoningEffort")}
            </div>
          )}
        </div>

        <div className="modal-actions">
          {terminal && (
            <button
              ref={reloadRef}
              type="button"
              className="btn btn-ghost"
              onClick={() => void reload()}
            >
              {t("models.settingsReload")}
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost"
            title={t("models.settingsRestoreHint")}
            disabled={busy || terminal}
            onClick={() => void restore()}
          >
            {lite("modelRestoreAll")}
          </button>
          <button type="button" className="btn btn-ghost" onClick={requestClose} disabled={busy}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || terminal}>
            {busy ? t("models.customSaving") : t("models.customApply")}
          </button>
        </div>
      </form>
    </dialog>
  );
}
