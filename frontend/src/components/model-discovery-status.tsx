import { Button } from "@/components/button";
import { useChannelCopy } from "@/features/channels/copy";
import { refreshChannelModels } from "@/features/channels/api";
import { useState } from "react";
import { useI18n } from "../i18n/shared";
import type { ProviderDiscoverySummary } from "../models-groups";
import { clientApplicationWarning } from "../contracts/application";
import { Notice } from "../ui";

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
  const c = useChannelCopy();
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [warning, setWarning] = useState<string>();
  if (!discovery?.checkedAt && apiBase === undefined) return null;
  const checked = discovery?.checkedAt ? new Date(discovery.checkedAt).toLocaleString() : null;
  const lastSuccess = discovery?.lastSuccessAt
    ? new Date(discovery.lastSuccessAt).toLocaleString()
    : null;
  const aliases = Object.entries(discovery?.staleAliases ?? {});
  const refresh = async () => {
    setBusy(true);
    setError("");
    setWarning(undefined);
    try {
      const result = await refreshChannelModels(apiBase!, provider!, t("refreshFailed"));
      if (!result) throw new Error(t("refreshFailed"));
      if (result.ok === false) setError(result.discovery?.error?.message || t("retainedUnknown"));
      setWarning(
        clientApplicationWarning(result.catalogRefresh, t("integrations.catalogRefreshSyncFailed")),
      );
      onRefreshed?.();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="model-discovery-status">
      {(discovery?.status === "not_checked" ||
        discovery?.status === "unsupported" ||
        discovery?.status === "refreshing") && (
        <p className="muted">
          {c(discovery.status === "not_checked" ? "never" : discovery.status)}
        </p>
      )}
      <div className="row">
        {checked && <p className="muted text-label">{t("lastChecked", { time: checked })}</p>}
        {apiBase !== undefined && provider && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => void refresh()}
          >
            {t(busy ? "refreshing" : "refresh")}
          </Button>
        )}
      </div>
      {error && <Notice tone="err">{error}</Notice>}
      {warning && <Notice tone="warn">{warning}</Notice>}
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
