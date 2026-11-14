import { useI18n } from "../i18n/shared";
import { useMigrationLabel } from "../runtime-capabilities";

export default function MigrationNotice({ title }: { title?: string }) {
  const { locale } = useI18n();
  const label = useMigrationLabel();
  return (
    <section className="card" role="status">
      <h2>{title ? `${title} · ${label}` : label}</h2>
      <p>
        {locale === "zh" || locale === "zh-TW"
          ? "此功能保留在迁移计划中，当前 Python 网关尚未提供。"
          : "This feature remains in the migration plan and is not available in the Python gateway yet."}
      </p>
    </section>
  );
}
