import { useCallback, useState } from "react";
import { useI18n } from "../i18n/shared";
import { liteTranslator } from "../i18n/lite-copy";
import { useDataSurface } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { formatBytes } from "../format-bytes";
import { Notice } from "../ui";
import { Button } from "../components/ui-v2/button";
import { SettingsSection } from "../components/v2-proxy/settings-layout";

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
const labels: Record<string, [string, string]> = {
  configuration: ["配置（可能包含 API Key）", "Configuration (may contain API keys)"],
  credentials: ["登录凭据", "Credentials"],
  clientRollback: ["客户端配置及恢复备份", "Client configuration and rollback backups"],
  history: ["订阅续聊历史", "Subscription continuation history"],
  usage: ["请求用量与审计", "Usage and audit records"],
  cache: ["可重新获取的缓存", "Refreshable cache"],
  logs: ["当前日志", "Current logs"],
  runtime: ["运行状态", "Runtime state"],
  other: ["其他保留数据", "Other retained data"],
};

export default function LiteStorage({ apiBase }: { apiBase: string }) {
  const { locale } = useI18n();
  const zh = locale === "zh" || locale === "zh-TW";
  const text = (cn: string, en: string) => (zh ? cn : en);
  const bytes = (value: number) => formatBytes(value, locale);
  const load = useCallback(
    async (signal: AbortSignal) =>
      readJsonOrThrow<Report>(
        await fetch(`${apiBase}/api/lite-storage`, { signal }),
        "Could not load storage.",
      ),
    [apiBase],
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
      text("操作失败。", "Operation failed."),
    );
    if (!result) throw new Error(text("响应为空。", "Empty response."));
    setMessage(liteTranslator(locale)("storageFilesProcessed", { count: String(result.count) }));
    if (!result.ok) setError(result.errors.map((row) => `${row.relPath}: ${row.error}`).join("; "));
    setPreview(null);
    resource.refresh();
  };
  const data = resource.state.data;
  return (
    <div className="dashboard-workspace-shell flex flex-col gap-5">
      <header>
        <h2 className="m-0 text-title font-semibold">{text("存储", "Storage")}</h2>
        <p className="text-fg-muted">
          {text(
            "管理 Lite 自身的数据。清理先预览，再移入可恢复的隔离区。",
            "Manage Lite's own data. Preview cleanup before moving files into recoverable quarantine.",
          )}
        </p>
      </header>
      {(error || resource.state.showError) && (
        <Notice tone="err">{error ?? text("无法加载存储信息。", "Could not load storage.")}</Notice>
      )}
      {message && <p role="status">{message}</p>}
      {!data && !resource.state.showError && <p role="status">{text("正在读取…", "Loading…")}</p>}
      {data && (
        <>
          <SettingsSection id="lite-storage-usage" title={text("数据占用", "Data usage")}>
            <div className="p-4">
              <p className="break-all text-sm text-fg-muted">{data.dataDirectory}</p>
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className="text-left">{text("类别", "Category")}</th>
                    <th>{text("文件", "Files")}</th>
                    <th>{text("大小", "Size")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.categories.map((row) => (
                    <tr key={row.id}>
                      <td className="py-2">{labels[row.id]?.[zh ? 0 : 1] ?? row.id}</td>
                      <td className="text-center">{row.count}</td>
                      <td className="text-center">{bytes(row.bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-sm text-fg-muted">
                {text(
                  "配置、凭据、客户端恢复备份、历史数据库与当前日志保留。用量数据库由自身的保留策略管理。",
                  "Configuration, credentials, client rollback backups, history databases and current logs are retained. The usage ledger manages its own retention policy.",
                )}
              </p>
              {data.usageRetention && (
                <p className="text-sm">
                  {text("用量数据库", "Usage ledger")}: {bytes(data.usageRetention.currentBytes)} ·{" "}
                  {data.usageRetention.maxBytes
                    ? text("上限", "Limit") + ": " + bytes(data.usageRetention.maxBytes)
                    : text("未设置容量上限", "No size limit configured")}
                </p>
              )}
              {data.errors.length > 0 && (
                <Notice tone="warn">
                  {text(
                    "部分文件无法读取，统计可能不完整。",
                    "Some files could not be read; totals may be incomplete.",
                  )}
                </Notice>
              )}
            </div>
          </SettingsSection>
          <SettingsSection
            id="lite-storage-cleanup"
            title={text("缓存清理", "Cache cleanup")}
            lede={text(
              "目前可清理 ChatGPT 额度快照；后续查询会重新获取。模型目录缓存在内存中。隔离文件仍占用磁盘空间。",
              "ChatGPT quota snapshots can be cleared and fetched again later. Model inventory is cached in memory. Quarantined files still occupy disk space.",
            )}
          >
            <div className="flex flex-col gap-3 p-4">
              <div>
                <Button
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      const result = await readJsonOrThrow<Preview>(
                        await fetch(`${apiBase}/api/lite-storage/preview`),
                        "Preview failed.",
                      );
                      if (!result) throw new Error("Empty preview.");
                      setPreview(result);
                    })
                  }
                >
                  {text("预览清理", "Preview cleanup")}
                </Button>
              </div>
              {preview && (
                <>
                  <p>
                    {preview.count} {text("个文件", "files")} · {bytes(preview.bytes)}
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
                      {text("确认移入隔离区", "Confirm quarantine")}
                    </Button>
                  </div>
                </>
              )}
            </div>
          </SettingsSection>
          <SettingsSection
            id="lite-storage-restore"
            title={text("恢复隔离文件", "Restore quarantined files")}
            lede={text(
              "已有同名文件时保留当前文件，恢复冲突会单独显示。",
              "Existing files are preserved; restore conflicts are reported individually.",
            )}
          >
            <div className="flex flex-col gap-3 p-4">
              {data.quarantine.length === 0 && (
                <p>{text("隔离区为空。", "Quarantine is empty.")}</p>
              )}
              {data.quarantine.map((row) => (
                <div key={row.id} className="flex flex-wrap items-center justify-between gap-3">
                  <span>
                    {row.createdAt ? new Date(row.createdAt * 1000).toLocaleString() : row.id} ·{" "}
                    {row.count ?? "—"} · {bytes(row.bytes ?? 0)} {row.error}
                  </span>
                  <Button
                    disabled={busy || !row.count || !!row.error}
                    onClick={() => void act(() => mutate(`restore/${row.id}`))}
                  >
                    {text("恢复", "Restore")}
                  </Button>
                </div>
              ))}
            </div>
          </SettingsSection>
        </>
      )}
      <div>
        <Button disabled={busy || resource.state.refreshing} onClick={() => resource.refresh()}>
          {text("刷新", "Refresh")}
        </Button>
      </div>
    </div>
  );
}
