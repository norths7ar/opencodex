import { Panel, PanelBody } from "@/components/settings/panel";
import { useCallback, useState } from "react";
import { PageHeader } from "../components/page-header";
import { Button } from "../components/button";
import { useDataSurface } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { formatBytes } from "../format-bytes";
import { useI18n, type TKey } from "../i18n/shared";
import { Notice } from "../ui";

interface Preview {
  digest: string;
  count: number;
  bytes: number;
  candidates: { relPath: string; bytes: number }[];
}
interface Report {
  dataDirectory: string;
  categories: { id: string; bytes: number; count: number; disposableBytes: number }[];
  quarantine: { id: string; createdAt?: number; count?: number; bytes?: number; error?: string }[];
  errors: string[];
  usageRetention?: { currentBytes: number; maxBytes?: number } | null;
}
interface Outcome {
  ok: boolean;
  count: number;
  errors: { relPath: string; error: string }[];
}
const labels = new Map<string, TKey>([
  ["configuration", "storage.category.configuration"],
  ["credentials", "storage.category.credentials"],
  ["clientRollback", "storage.category.clientRollback"],
  ["history", "storage.category.history"],
  ["usage", "storage.category.usage"],
  ["cache", "storage.category.cache"],
  ["logs", "storage.category.logs"],
  ["runtime", "storage.category.runtime"],
  ["other", "storage.category.other"],
]);

export default function LiteStorage({ apiBase }: { apiBase: string }) {
  const { locale, t } = useI18n();
  const bytes = (value: number) => formatBytes(value, locale);
  const load = useCallback(
    async (signal: AbortSignal) =>
      readJsonOrThrow<Report>(
        await fetch(`${apiBase}/api/lite-storage`, { signal }),
        t("storage.loadFailed"),
      ),
    [apiBase, t],
  );
  const resource = useDataSurface(`lite-storage:${apiBase}`, [apiBase], load, {
    isEmpty: () => false,
  });
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await action();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  const mutate = async (path: string, body?: object) => {
    const result = await readJsonOrThrow<Outcome>(
      await fetch(`${apiBase}/api/lite-storage/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      }),
      t("storage.operationFailed"),
    );
    if (!result) throw new Error(t("storage.emptyResponse"));
    setMessage(t("storageFilesProcessed", { count: String(result.count) }));
    if (!result.ok) setError(result.errors.map((row) => `${row.relPath}: ${row.error}`).join("; "));
    setPreview(null);
    resource.refresh();
  };
  const data = resource.state.data;
  return (
    <div className="dashboard-workspace-shell flex flex-col gap-5">
      <PageHeader title={t("nav.storage")} subtitle={t("storage.subtitle")} />
      {(error || resource.state.showError) && (
        <Notice tone="err">{error ?? t("storage.loadFailed")}</Notice>
      )}
      {message && <p role="status">{message}</p>}
      {!data && !resource.state.showError && <p role="status">{t("common.loading")}</p>}
      {data && (
        <>
          <Panel id="lite-storage-usage" title={t("storage.dataUsage")}>
            <PanelBody>
              <div className="p-4">
                <p className="break-all text-sm text-fg-muted">{data.dataDirectory}</p>
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="text-left">{t("storage.category")}</th>
                      <th>{t("storage.files")}</th>
                      <th>{t("storage.size")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.categories.map((row) => {
                      const label = labels.get(row.id);
                      return (
                        <tr key={row.id}>
                          <td className="py-2">{label ? t(label) : row.id}</td>
                          <td className="text-center">{row.count}</td>
                          <td className="text-center">{bytes(row.bytes)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p className="text-sm text-fg-muted">{t("storage.retentionDescription")}</p>
                {data.usageRetention && (
                  <p className="text-sm">
                    {t("storage.usageLedger")}: {bytes(data.usageRetention.currentBytes)} ·{" "}
                    {data.usageRetention.maxBytes
                      ? t("storage.sizeLimit", { size: bytes(data.usageRetention.maxBytes) })
                      : t("storage.noSizeLimit")}
                  </p>
                )}
                {data.errors.length > 0 && (
                  <Notice tone="warn">{t("storage.incompleteTotals")}</Notice>
                )}
              </div>
            </PanelBody>
          </Panel>
          <Panel
            id="lite-storage-cleanup"
            title={t("storage.cacheCleanup")}
            description={t("storage.cacheCleanupDescription")}
          >
            <PanelBody>
              <div className="flex flex-col gap-3 p-4">
                <div>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        const result = await readJsonOrThrow<Preview>(
                          await fetch(`${apiBase}/api/lite-storage/preview`),
                          t("storage.previewFailed"),
                        );
                        if (!result) throw new Error(t("storage.emptyPreview"));
                        setPreview(result);
                      })
                    }
                  >
                    {t("storage.previewCleanup")}
                  </Button>
                </div>
                {preview && (
                  <>
                    <p>
                      {t("storage.fileCount", { count: String(preview.count) })} ·{" "}
                      {bytes(preview.bytes)}
                    </p>
                    <ul>
                      {preview.candidates.map((row) => (
                        <li key={row.relPath}>
                          {row.relPath} · {bytes(row.bytes)}
                        </li>
                      ))}
                    </ul>
                    <div>
                      <Button
                        disabled={busy || preview.count === 0}
                        onClick={() =>
                          void act(() => mutate("quarantine", { digest: preview.digest }))
                        }
                      >
                        {t("storage.confirmQuarantine")}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            </PanelBody>
          </Panel>
          <Panel
            id="lite-storage-restore"
            title={t("storage.restoreQuarantined")}
            description={t("storage.restoreDescription")}
          >
            <PanelBody>
              <div className="flex flex-col gap-3 p-4">
                {data.quarantine.length === 0 && <p>{t("storage.quarantineEmpty")}</p>}
                {data.quarantine.map((row) => (
                  <div key={row.id} className="flex flex-wrap items-center justify-between gap-3">
                    <span>
                      {row.createdAt
                        ? new Date(row.createdAt * 1000).toLocaleString(locale)
                        : row.id}{" "}
                      · {row.count ?? "—"} · {bytes(row.bytes ?? 0)} {row.error}
                    </span>
                    <Button
                      disabled={busy || !row.count || !!row.error}
                      onClick={() => void act(() => mutate(`restore/${row.id}`))}
                    >
                      {t("storage.restore")}
                    </Button>
                  </div>
                ))}
              </div>
            </PanelBody>
          </Panel>
        </>
      )}
      <div>
        <Button disabled={busy || resource.state.refreshing} onClick={() => resource.refresh()}>
          {t("channels.refresh")}
        </Button>
      </div>
    </div>
  );
}
