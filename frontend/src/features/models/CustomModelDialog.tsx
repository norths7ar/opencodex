import { Button } from "@/components/button";
import type { ApplicationReceipt } from "@/contracts/channels";
import { readJsonOrThrow } from "@/fetch-json";
import { useI18n, type TKey } from "@/i18n/shared";
import {
  CUSTOM_OPTION,
  REASONING_EFFORT_LEVELS,
  parseContextWindowDraft,
  type ModelRow,
} from "@/pages/models-shared";
import { formatProviderDisplayName } from "@/provider-icons";
import { Notice, Select } from "@/ui";
import { useRef, useState } from "react";

export function CustomModelDialog({
  apiBase,
  provider: customModalProvider,
  models,
  onClose,
  onSaved,
}: {
  apiBase: string;
  provider: string;
  models: ModelRow[];
  onClose: () => void;
  onSaved: (application: ApplicationReceipt | undefined) => Promise<void>;
}) {
  const { t } = useI18n();
  const [customFormModelId, setCustomFormModelId] = useState("");
  const [customFormDisplayName, setCustomFormDisplayName] = useState("");
  const [customFormContextWindow, setCustomFormContextWindow] = useState("");
  const [customFormShowCustomCtx, setCustomFormShowCustomCtx] = useState(false);
  const [customFormModalities, setCustomFormModalities] = useState<string[]>(["text"]);
  const [customFormReasoning, setCustomFormReasoning] = useState(false);
  const [customFormReasoningEfforts, setCustomFormReasoningEfforts] = useState<string[]>([]);
  // Whether the ladder has been seeded at least once. `[]` is a MEANINGFUL explicit
  // no-reasoning override, so initialization is tracked separately from the array contents:
  // once seeded (an edit's stored ladder — including an explicit empty one — or a new form's
  // first enable), re-enabling the override preserves the current array even when empty.
  const customFormReasoningInitializedRef = useRef(false);
  const [customSaving, setCustomSaving] = useState(false);
  const [customError, setCustomError] = useState("");
  const addCustomModel = async (
    provider: string,
    modelId: string,
    displayName?: string,
    contextWindow?: number,
    inputModalities?: string[],
    reasoningEfforts?: string[],
  ) => {
    setCustomSaving(true);
    setCustomError("");
    try {
      const r = await fetch(`${apiBase}/api/custom-models`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          modelId,
          displayName,
          contextWindow,
          inputModalities,
          reasoningEfforts,
        }),
      });
      try {
        const result = await readJsonOrThrow<{ catalogRefresh: ApplicationReceipt }>(
          r,
          t("models.customSaveFailed"),
        );
        onClose();
        await onSaved(result?.catalogRefresh);
      } catch (e) {
        setCustomError(e instanceof Error ? e.message : t("models.customSaveFailed"));
      }
    } catch {
      setCustomError(t("models.networkError"));
    } finally {
      setCustomSaving(false);
    }
  };

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t("models.customAdd")}
      onClick={() => {
        if (!customSaving) onClose();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && !customSaving) onClose();
      }}
    >
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>
            {t("models.customAddTitle", {
              provider: formatProviderDisplayName(customModalProvider, t),
            })}
          </h3>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onClose()}
            disabled={customSaving}
            aria-label={t("common.close")}
          >
            &times;
          </Button>
        </div>

        {customError && <Notice tone="err">{customError}</Notice>}

        <div className="models-field-stack">
          <label className="text-label models-field">
            {t("models.customFieldModelId")}
            <input
              className="input"
              value={customFormModelId}
              onChange={(e) => setCustomFormModelId(e.target.value)}
              disabled={customSaving}
              placeholder={t("models.customFieldModelIdPlaceholder")}
              autoFocus
            />
          </label>

          <label className="text-label models-field">
            {t("models.customFieldDisplayName")}
            <input
              className="input"
              value={customFormDisplayName}
              onChange={(e) => setCustomFormDisplayName(e.target.value)}
              disabled={customSaving}
              placeholder={t("models.customFieldDisplayNamePlaceholder")}
            />
          </label>

          <label className="text-label models-field">
            {t("models.customFieldContext")}
            <div className="row models-field-row">
              <Select
                value={customFormShowCustomCtx ? CUSTOM_OPTION : customFormContextWindow}
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
                onChange={(v) => {
                  if (v === CUSTOM_OPTION) {
                    setCustomFormShowCustomCtx(true);
                    return;
                  }
                  setCustomFormShowCustomCtx(false);
                  setCustomFormContextWindow(v);
                }}
                disabled={customSaving}
                label={t("models.customFieldContext")}
              />
              {customFormShowCustomCtx && (
                <input
                  className="input"
                  style={{ width: 120 }}
                  inputMode="numeric"
                  value={customFormContextWindow}
                  onChange={(e) => setCustomFormContextWindow(e.target.value)}
                  disabled={customSaving}
                  placeholder={t("models.customPlaceholder")}
                  aria-label={t("models.customFieldContext")}
                />
              )}
            </div>
          </label>

          <div className="text-label models-field">
            {t("models.customFieldModalities")}
            <div className="row models-field-row">
              {(["text", "image", "audio", "video"] as const).map((mod) => (
                <label key={mod} className="row models-modality-option">
                  <input
                    type="checkbox"
                    checked={customFormModalities.includes(mod)}
                    onChange={(e) => {
                      setCustomFormModalities((prev) =>
                        e.target.checked ? [...prev, mod] : prev.filter((m) => m !== mod),
                      );
                    }}
                    disabled={customSaving}
                  />
                  <span className="text-control">{mod}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="text-label models-field">
            {t("models.customFieldReasoning")}
            <div className="row models-field-row">
              <label className="row models-modality-option">
                <input
                  type="checkbox"
                  checked={customFormReasoning}
                  onChange={(e) => {
                    setCustomFormReasoning(e.target.checked);
                    if (e.target.checked && !customFormReasoningInitializedRef.current) {
                      customFormReasoningInitializedRef.current = true;
                      // First enable: seed from the model's advertised ladder when the
                      // row is known (a provider may support only a subset of levels —
                      // preselecting the full shared list would persist levels the model
                      // does not accept). Unknown model ids fall back to the full set:
                      // the common intent of enabling the override is "allow every known
                      // step", and the wire clamp still bounds what is actually sent.
                      const row = models.find(
                        (m) => m.provider === customModalProvider && m.id === customFormModelId,
                      );
                      const advertised = Array.isArray(row?.reasoningEfforts)
                        ? row.reasoningEfforts
                        : undefined;
                      setCustomFormReasoningEfforts(advertised ?? [...REASONING_EFFORT_LEVELS]);
                    }
                  }}
                  disabled={customSaving}
                />
                <span className="text-control">{t("models.customFieldReasoningOverride")}</span>
              </label>
            </div>
            {customFormReasoning && (
              <div className="row models-field-row" style={{ flexWrap: "wrap" }}>
                {REASONING_EFFORT_LEVELS.map((effort) => (
                  <label key={effort} className="row models-modality-option">
                    <input
                      type="checkbox"
                      checked={customFormReasoningEfforts.includes(effort)}
                      onChange={(e) => {
                        setCustomFormReasoningEfforts((prev) =>
                          e.target.checked
                            ? [...prev, effort]
                            : prev.filter((level) => level !== effort),
                        );
                      }}
                      disabled={customSaving}
                    />
                    <span className="text-control">
                      {t(`models.reasoningEffort.${effort}` as TKey)}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="modal-actions">
          <Button
            type="button"
            variant="ghost"
            size="md"
            onClick={() => onClose()}
            disabled={customSaving}
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            variant="primary"
            size="md"
            disabled={customSaving || !customFormModelId.trim()}
            onClick={() => {
              const modelId = customFormModelId.trim();
              const displayName = customFormDisplayName.trim();
              const parsedContextWindow = parseContextWindowDraft(customFormContextWindow); // "350k" -> undefined, never "omitted / cleared"
              if (parsedContextWindow === undefined) {
                setCustomError(t("models.contextInvalid"));
                return;
              }
              void addCustomModel(
                customModalProvider,
                modelId,
                displayName || undefined,
                parsedContextWindow ?? undefined,
                customFormModalities,
                customFormReasoning ? customFormReasoningEfforts : undefined,
              );
            }}
          >
            {t(customSaving ? "models.customSaving" : "models.customAddBtn")}
          </Button>
        </div>
      </div>
    </div>
  );
}
