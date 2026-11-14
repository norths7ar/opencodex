import type { ProviderDiscoverySummary } from "../models-groups";
import { useI18n } from "../i18n/shared";
import { Notice } from "../ui";
import { useState } from "react";
import { apiErrorMessage } from "../api-error";
import { liteTranslator } from "../i18n/lite-copy";

export function ModelDiscoveryStatus({
  discovery,
  apiBase,
  provider,
  onRefreshed,
}: {
  discovery?: ProviderDiscoverySummary;
  apiBase?: string;
  provider?: string;
  onRefreshed?: () => void;
}) {
  const { locale } = useI18n();
  const t = liteTranslator(locale);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!discovery?.checkedAt && apiBase === undefined) return null;
  const checked = discovery?.checkedAt ? new Date(discovery.checkedAt).toLocaleString() : null;
  const lastSuccess = discovery?.lastSuccessAt
    ? new Date(discovery.lastSuccessAt).toLocaleString()
    : null;
  const aliases = Object.entries(discovery?.staleAliases ?? {});
  const refresh = async () => {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `${apiBase}/api/providers/${encodeURIComponent(provider!)}/models/refresh`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await apiErrorMessage(response, t("refreshFailed")));
      const result = (await response.json()) as { ok?: boolean; discovery?: { error?: string } };
      if (result.ok === false) setError(result.discovery?.error || t("retainedUnknown"));
      onRefreshed?.();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="model-discovery-status">
      <div className="row">
        {checked && <p className="muted text-label">{t("lastChecked", { time: checked })}</p>}
        {apiBase !== undefined && provider && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => void refresh()}
          >
            {t(busy ? "refreshing" : "refresh")}
          </button>
        )}
      </div>
      {error && <Notice tone="err">{error}</Notice>}
      {discovery?.status === "failed" && (
        <Notice tone="warn">
          {t("refreshFailed")}
          {lastSuccess && <div>{t("retained", { time: lastSuccess })}</div>}
          {discovery.error && <div>{discovery.error}</div>}
        </Notice>
      )}
      {discovery?.staleDefaultModel || aliases.length ? (
        <Notice tone="warn">
          {discovery?.staleDefaultModel && (
            <div>
              {t("staleDefault")}
              <code>{discovery.staleDefaultModel}</code>
            </div>
          )}
          {aliases.length > 0 && (
            <div>
              {t("staleAliases")}
              {aliases.map(([model, alias]) => `${alias} → ${model}`).join(", ")}
            </div>
          )}
          <div>{t("chooseTarget")}</div>
        </Notice>
      ) : null}
    </div>
  );
}
