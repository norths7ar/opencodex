import { useI18n, type TKey } from "../i18n/shared";
import type {
  ModelEffectiveCost,
  ModelMetadataSource,
  ModelPricingSchedule,
  ModelRow,
} from "../pages/models-shared";
import { RATE_FIELDS } from "./model-price-cost";

const RATE_LABELS = {
  input: "pricing.override.input",
  output: "pricing.override.output",
  cacheRead: "pricing.override.cacheRead",
  cacheWrite: "pricing.override.cacheWrite",
} as const;

const SOURCE_LABELS = {
  operator: "modelSourceOperator",
  upstream: "modelSourceUpstream",
  official: "modelSourceOfficial",
  preset: "modelSourcePreset",
  fallback: "modelSourceFallback",
  provider: "modelSourceProvider",
} as const;

export function MetadataSource({
  value,
  reset = false,
  linked = false,
}: {
  value?: ModelMetadataSource;
  reset?: boolean;
  linked?: boolean;
}) {
  const { t } = useI18n();
  if (reset) return <span className="muted text-caption">{t("modelResetOnSave")}</span>;
  const label = t(
    SOURCE_LABELS[value?.source as keyof typeof SOURCE_LABELS] ?? "modelSourceAutomatic",
  );
  const checked = value?.updatedAt ? new Date(value.updatedAt) : undefined;
  const date = checked && Number.isFinite(checked.getTime()) ? checked.toLocaleString() : undefined;
  return (
    <span className="muted text-caption">
      {linked && value?.sourceRef && /^https?:\/\//i.test(value.sourceRef) ? (
        <a href={value.sourceRef} target="_blank" rel="noreferrer">
          {label}
        </a>
      ) : (
        label
      )}
      {date && <> · {date}</>}
      {value?.stale && <> · {t("modelStaleSource")}</>}
    </span>
  );
}

export function ModelCostSummary({
  cost,
  subscription,
  schedule,
  linked = false,
}: {
  cost?: ModelEffectiveCost;
  subscription?: boolean;
  schedule?: ModelPricingSchedule;
  linked?: boolean;
}) {
  const { t } = useI18n();
  if (subscription) return <>{t("modelNotApplicable")}</>;
  if (!cost && !schedule) return <>{t("modelUnknown")}</>;
  const rates = (values: ModelEffectiveCost) =>
    RATE_FIELDS.map((field) => `${t(RATE_LABELS[field])}: ${values[field] ?? "—"}`).join(" · ");
  const prompt = cost?.promptLengthPricing;
  return (
    <>
      <span className="model-summary-line">{t("modelPricingUnit")}</span>
      {cost && <span className="model-summary-line">{rates(cost)}</span>}
      {prompt?.mode === "custom" && (
        <span className="model-summary-line">
          {prompt.comparison === "gt" ? ">" : "≥"} {prompt.thresholdTokens.toLocaleString()}:{" "}
          {rates(prompt.rates)}
        </span>
      )}
      {schedule && !cost && (
        <>
          <span className="model-summary-line">
            {t("modelPricingPeak")}: {rates(schedule.bands.peak)}
          </span>
          <span className="model-summary-line">
            {t("modelPricingOffPeak")}: {rates(schedule.bands.offPeak)}
          </span>
          {schedule.condition && <span className="model-summary-line">{schedule.condition}</span>}
          <MetadataSource
            linked={linked}
            value={{
              source: "official",
              sourceRef: schedule.sourceRef,
              updatedAt: schedule.verifiedAt,
              stale: schedule.stale,
            }}
          />
          <span className="model-summary-line muted text-caption">
            {t("modelPricingScheduleNote")}
          </span>
        </>
      )}
    </>
  );
}

export function ModelReasoningSummary({ row }: { row: ModelRow }) {
  const { t } = useI18n();
  const levels = row.reasoningEfforts ?? [];
  const effort = (value: string) => t(("models.reasoningEffort." + value) as TKey);
  return (
    <>
      {levels.length
        ? levels.map(effort).join(", ")
        : t(
            row.supportsReasoning === undefined && !row.reasoningEfforts
              ? "modelUnknown"
              : row.supportsReasoning
                ? "modelReasoningSupported"
                : "modelReasoningDisabled",
          )}
      {row.defaultReasoningEffort && (
        <span className="model-summary-line">
          {t("models.settingsDefaultEffort")}: {effort(row.defaultReasoningEffort)}
        </span>
      )}
    </>
  );
}
