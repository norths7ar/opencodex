import { useId, type Dispatch, type RefObject, type SetStateAction } from "react";
import "./model-price-dialog.css";
import { useT, type TKey } from "../i18n/shared";
import { Select } from "../ui";
import {
  CUSTOM_FIELD,
  RATE_FIELDS,
  type CustomDraft,
  type InvalidField,
  type RateDraft,
  type PricingMode,
  type RateField,
} from "./model-price-cost";

const RATE_LABELS: Record<RateField, TKey> = {
  input: "pricing.override.input",
  output: "pricing.override.output",
  cacheRead: "pricing.override.cacheRead",
  cacheWrite: "pricing.override.cacheWrite",
};

interface ModelPriceFieldsProps {
  draft: RateDraft;
  setDraft: Dispatch<SetStateAction<RateDraft>>;
  pricingMode: PricingMode;
  setPricingMode: Dispatch<SetStateAction<PricingMode>>;
  customDraft: CustomDraft;
  setCustomDraft: Dispatch<SetStateAction<CustomDraft>>;
  locked: boolean;
  invalidField: InvalidField | null;
  customFieldRefs: RefObject<Partial<Record<InvalidField, HTMLInputElement>>>;
  onEdit: () => void;
}

export default function ModelPriceFields({
  draft,
  setDraft,
  pricingMode,
  setPricingMode,
  customDraft,
  setCustomDraft,
  locked,
  invalidField,
  customFieldRefs,
  onEdit,
}: ModelPriceFieldsProps) {
  const t = useT();
  const id = useId();
  const helpId = useId();
  return (
    <>
      <p id={helpId} className="muted small">
        {t("pricing.override.help")}
      </p>
      <p className="muted small">{t("pricing.override.promptHelp")}</p>
      <div className="model-price-rates-grid">
        {RATE_FIELDS.map((field) => (
          <div className="model-price-rate-field" key={field}>
            <label className="field-label" htmlFor={`${id}-${field}`}>
              {t(RATE_LABELS[field])}
            </label>
            <input
              ref={(node) => {
                if (node) customFieldRefs.current[field] = node;
                else delete customFieldRefs.current[field];
              }}
              id={`${id}-${field}`}
              className="input"
              type="number"
              min={0}
              max={1_000_000}
              step="any"
              inputMode="decimal"
              value={draft[field]}
              disabled={locked}
              required={field === "input" || field === "output"}
              aria-describedby={helpId}
              aria-invalid={invalidField === field ? true : undefined}
              onChange={(event) => {
                if (locked) return;
                const value = event.target.value;
                setDraft((current) => ({
                  ...current,
                  cacheRead: current.cacheRead || "0",
                  cacheWrite: current.cacheWrite || "0",
                  [field]: value,
                }));
                onEdit();
              }}
            />
          </div>
        ))}
      </div>
      <fieldset className="model-price-mode">
        <legend className="field-label">{t("pricing.override.promptTitle")}</legend>
        {(["automatic", "flat", "custom"] as const).map((mode) => (
          <label className="model-price-mode-option" key={mode}>
            <input
              type="radio"
              name={`${id}-mode`}
              value={mode}
              checked={pricingMode === mode}
              disabled={locked}
              onChange={() => {
                setPricingMode(mode);
                onEdit();
              }}
            />
            <span>
              <strong>
                {t(`pricing.override.mode${mode[0]!.toUpperCase()}${mode.slice(1)}` as TKey)}
              </strong>
              <small className="muted">
                {t(`pricing.override.mode${mode[0]!.toUpperCase()}${mode.slice(1)}Help` as TKey)}
              </small>
            </span>
          </label>
        ))}
      </fieldset>
      {pricingMode === "custom" && (
        <section className="model-price-band" aria-labelledby={`${id}-custom-title`}>
          <h4 id={`${id}-custom-title`} className="model-price-band-title">
            {t("pricing.override.bandTitle")}
          </h4>
          <div className="model-price-custom-controls">
            <div>
              <label className="field-label" htmlFor={`${id}-threshold`}>
                {t("pricing.override.threshold")}
              </label>
              <input
                ref={(node) => {
                  if (node) customFieldRefs.current.threshold = node;
                  else delete customFieldRefs.current.threshold;
                }}
                id={`${id}-threshold`}
                className="input"
                type="number"
                min={1}
                step={1}
                inputMode="numeric"
                value={customDraft.threshold}
                disabled={locked}
                aria-describedby={helpId}
                aria-invalid={invalidField === "threshold" ? true : undefined}
                onChange={(event) => {
                  setCustomDraft((current) => ({ ...current, threshold: event.target.value }));
                  onEdit();
                }}
              />
            </div>
            <div>
              <label className="field-label" htmlFor={`${id}-comparison`}>
                {t("pricing.override.comparison")}
              </label>
              <Select
                id={`${id}-comparison`}
                label={t("pricing.override.comparison")}
                value={customDraft.comparison}
                disabled={locked}
                portal={false}
                describedBy={helpId}
                options={[
                  { value: "gt", label: t("pricing.override.comparisonGt") },
                  { value: "gte", label: t("pricing.override.comparisonGte") },
                ]}
                onChange={(value) => {
                  setCustomDraft((current) => ({
                    ...current,
                    comparison: value as "gt" | "gte",
                  }));
                  onEdit();
                }}
              />
            </div>
          </div>
          <p id={`${id}-custom-help`} className="muted small">
            {t("pricing.override.bandHelp")}
          </p>
          <div className="model-price-rates-grid">
            {RATE_FIELDS.map((field) => (
              <div className="model-price-rate-field" key={`custom-${field}`}>
                <label className="field-label" htmlFor={`${id}-custom-${field}`}>
                  {t(RATE_LABELS[field])}
                </label>
                <input
                  ref={(node) => {
                    const key = CUSTOM_FIELD[field];
                    if (node) customFieldRefs.current[key] = node;
                    else delete customFieldRefs.current[key];
                  }}
                  id={`${id}-custom-${field}`}
                  className="input"
                  type="number"
                  min={0}
                  max={1_000_000}
                  step="any"
                  inputMode="decimal"
                  value={customDraft[field]}
                  disabled={locked}
                  required={field === "input" || field === "output"}
                  aria-describedby={helpId}
                  aria-invalid={invalidField === CUSTOM_FIELD[field] ? true : undefined}
                  onChange={(event) => {
                    const value = event.target.value;
                    setCustomDraft((current) => ({ ...current, [field]: value }));
                    onEdit();
                  }}
                />
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
