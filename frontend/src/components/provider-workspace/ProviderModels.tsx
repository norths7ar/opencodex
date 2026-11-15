/** Read-only channel inventory; the Models workspace owns model edits. */
import { useState } from "react";
import { useI18n, useT } from "../../i18n/shared";
import type { WorkspaceItem } from "../../provider-workspace/catalog";
import type { ModelRow } from "../../pages/models-shared";

type ProviderModelsProps = {
  item: WorkspaceItem;
  apiBase: string;
  availableModels: string[];
  hasLiveModels: boolean;
  selectedModels: string[];
  modelRows: ModelRow[] | null;
  modelRevision: string;
  modelRowsReady: boolean;
  modelsLoading?: boolean;
  modelsLoadFailed?: boolean;
  needsReauth?: boolean;
  onRetryModels?: () => void;
  onOpenAccounts?: () => void;
  onOpenModels: () => void;
};

export default function ProviderModels({
  item,
  availableModels,
  modelRows,
  modelRowsReady,
  modelsLoadFailed,
  needsReauth,
  onRetryModels,
  onOpenAccounts,
  onOpenModels,
}: ProviderModelsProps) {
  const t = useT();
  const { locale } = useI18n();
  const chinese = locale === "zh" || locale === "zh-TW";
  const [query, setQuery] = useState("");
  const rows = modelRows?.filter((row) => row.provider === item.name);
  const inventory = rows
    ? rows.map((row) => ({ id: row.id, hidden: row.disabled }))
    : [...new Set(availableModels)].map((id) => ({ id, hidden: false }));
  const filtered = inventory.filter((row) =>
    row.id.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const ready = modelRowsReady || inventory.length > 0;
  return (
    <section
      className="flex flex-col gap-4"
      aria-label={t("pws.tab.models")}
      aria-busy={!ready || undefined}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h3 className="m-0 text-[15px] font-semibold">{t("pws.tab.models")}</h3>
          <span className="text-[13px] text-fg-muted">{ready ? inventory.length : "\u00a0"}</span>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenModels}>
          {chinese ? "管理模型" : "Manage models"}
        </button>
      </div>
      {needsReauth && (
        <div className="pws-inline-error" role="status">
          <span>{t("pws.modelsNeedsReauth")}</span>
          {onOpenAccounts && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenAccounts}>
              {t("pws.tab.accounts")}
            </button>
          )}
        </div>
      )}
      {modelsLoadFailed && (
        <div className="pws-inline-error" role="alert">
          <span>{t("pws.modelsLoadFailed")}</span>
          {onRetryModels && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onRetryModels}>
              {t("common.retry")}
            </button>
          )}
        </div>
      )}
      {inventory.length > 8 && (
        <input
          type="search"
          className="input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("pws.modelSearchPlaceholder")}
          aria-label={t("pws.modelSearchPlaceholder")}
        />
      )}
      {!ready ? (
        <div className="min-h-24" />
      ) : filtered.length === 0 ? (
        <p className="muted">{t(inventory.length ? "pws.noModelMatch" : "pws.noModels")}</p>
      ) : (
        <ul className="m-0 flex list-none flex-col divide-y divide-line overflow-hidden rounded-xl border border-line bg-raised p-0">
          {filtered.slice(0, 300).map((row) => (
            <li
              key={row.id}
              className="flex min-h-12 flex-wrap items-center justify-between gap-3 px-4 py-3"
            >
              <code className="min-w-0 break-all text-[13px]">{row.id}</code>
              <span className="flex items-center gap-2 text-xs text-fg-muted">
                {row.id === item.defaultModel && (
                  <span className="badge badge-muted">{t("prov.defaultBadge")}</span>
                )}
                {row.hidden && <span>{chinese ? "已隐藏" : "Hidden"}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
      {filtered.length > 300 && (
        <p className="muted text-label">
          {t("pws.modelsTruncated", { shown: "300", total: String(filtered.length) })}
        </p>
      )}
    </section>
  );
}
