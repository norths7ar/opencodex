import { useI18n } from "../i18n/shared";

/** A retained feature entry whose implementation is scheduled separately. */
export function PlannedFeature({
  title,
  description,
  compact = false,
}: {
  title: string;
  description: string;
  compact?: boolean;
}) {
  const { locale } = useI18n();
  return (
    <section
      className={`rounded-xl border border-line bg-surface ${compact ? "p-4" : "p-6 sm:p-8"}`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="m-0 text-[15px] font-semibold text-fg">{title}</h3>
        <span className="rounded-full bg-raised px-2.5 py-1 text-xs text-fg-muted">
          {locale === "zh" ? "待实现" : locale === "zh-TW" ? "待實作" : "Planned"}
        </span>
      </div>
      <p className="mt-3 mb-0 max-w-[60ch] text-[13px] leading-6 text-fg-muted">{description}</p>
    </section>
  );
}
