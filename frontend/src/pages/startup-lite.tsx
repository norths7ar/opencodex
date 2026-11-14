import { useCallback, useState } from "react";
import { useI18n } from "../i18n/shared";
import { useDataSurface } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { DataSurfaceSkeleton } from "../components/data-surface";
import {
  Banner,
  CommandRow,
  PageHeader,
  Panel,
  StateBadge,
} from "../components/v2-startup/primitives";
import { Button } from "../components/ui-v2/button";

interface StartupHealth {
  taskName: string;
  stateDirectory: string;
  runtimeState: string;
  logPath: string;
  supervisorLogPath: string;
  serviceInstalled: boolean;
  serviceEnabled: boolean;
  serviceViable: boolean;
  serviceStale: boolean;
  serviceConflict: boolean;
  diagnosticStale: boolean;
  error?: string | null;
  commands: Record<string, string>;
}

export default function LiteStartup({ apiBase }: { apiBase: string }) {
  const { locale } = useI18n();
  const zh = locale === "zh" || locale === "zh-TW";
  const copy = (en: string, cn: string) => (zh ? cn : en);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const load = useCallback(
    async (signal: AbortSignal) => {
      const response = await fetch(`${apiBase}/api/startup-health`, { signal });
      return readJsonOrThrow<StartupHealth>(response, "Could not read automatic startup status.");
    },
    [apiBase],
  );
  const resource = useDataSurface(`lite-startup:${apiBase}`, [apiBase], load, {
    isEmpty: () => false,
  });
  const data = resource.state.data;
  const blocked =
    busy || resource.state.refreshing || !data || data.diagnosticStale || data.serviceConflict;

  const act = async (action: string) => {
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch(`${apiBase}/api/startup-action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      await readJsonOrThrow(
        response,
        copy("Could not change automatic startup.", "无法更改自动启动设置。"),
      );
      setNotice({ error: false, text: copy("Automatic startup updated.", "自动启动设置已更新。") });
      resource.refresh();
    } catch (error) {
      setNotice({ error: true, text: error instanceof Error ? error.message : String(error) });
      resource.refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="startup-page flex flex-col gap-5">
      <PageHeader
        title={copy("Startup", "启动")}
        subtitle={copy(
          "Keep the local gateway available after signing in to Windows.",
          "登录 Windows 后自动运行本地网关。",
        )}
        actions={
          <Button disabled={busy || resource.state.refreshing} onClick={() => resource.refresh()}>
            {copy("Refresh", "刷新")}
          </Button>
        }
      />
      {resource.state.showError && (
        <Banner tone="err">
          {copy(
            "Could not read startup status. Refresh to retry.",
            "无法读取启动状态，请刷新重试。",
          )}
        </Banner>
      )}
      {!data && !resource.state.showError && (
        <DataSurfaceSkeleton label={copy("Loading startup status", "正在读取启动状态")} rows={3} />
      )}
      {notice && <Banner tone={notice.error ? "err" : "ok"}>{notice.text}</Banner>}
      {data && (
        <>
          {data.error && <Banner tone="warn">{data.error}</Banner>}
          <Panel
            title={copy("Automatic background startup", "自动后台启动")}
            meta={
              <StateBadge tone={data.serviceViable ? "ok" : "warn"}>
                {data.diagnosticStale
                  ? copy("Status unavailable", "状态未知")
                  : data.serviceViable
                    ? copy("Enabled", "已启用")
                    : data.serviceStale
                      ? copy("Repair needed", "需要修复")
                      : copy("Not enabled", "未启用")}
              </StateBadge>
            }
            description={copy(
              "Runs as your Windows account at login, reconnect and unlock. The gateway stays running when the browser or Service Desk closes. Unexpected process exits are retried; an explicit Stop is respected.",
              "在当前 Windows 账号登录、重新连接或解锁时启动。关闭浏览器或 Service Desk 不影响网关。进程意外退出后自动重试；手动停止则保持停止。",
            )}
            actions={
              <div className="flex flex-wrap gap-2">
                {!data.serviceInstalled && (
                  <Button variant="primary" disabled={blocked} onClick={() => void act("install")}>
                    {copy("Enable automatic startup", "启用自动启动")}
                  </Button>
                )}
                {data.serviceInstalled && (
                  <>
                    <Button
                      disabled={blocked}
                      onClick={() => void act(data.serviceEnabled ? "disable" : "enable")}
                    >
                      {data.serviceEnabled ? copy("Disable", "禁用") : copy("Enable", "启用")}
                    </Button>
                    <Button disabled={blocked} onClick={() => void act("repair")}>
                      {copy("Repair", "修复")}
                    </Button>
                    <Button variant="danger" disabled={blocked} onClick={() => void act("remove")}>
                      {copy("Remove registration", "移除启动注册")}
                    </Button>
                  </>
                )}
              </div>
            }
          >
            <dl className="grid gap-2 text-sm">
              <div>
                <dt className="text-fg-muted">{copy("Gateway", "网关")}</dt>
                <dd className="m-0">{data.runtimeState}</dd>
              </div>
              <div>
                <dt className="text-fg-muted">{copy("Startup task", "启动任务")}</dt>
                <dd className="m-0 break-all">{data.taskName}</dd>
              </div>
              <div>
                <dt className="text-fg-muted">{copy("Data directory", "数据目录")}</dt>
                <dd className="m-0 break-all">{data.stateDirectory}</dd>
              </div>
              <div>
                <dt className="text-fg-muted">{copy("Runtime log", "运行日志")}</dt>
                <dd className="m-0 break-all">{data.logPath}</dd>
              </div>
              <div>
                <dt className="text-fg-muted">{copy("Startup log", "启动日志")}</dt>
                <dd className="m-0 break-all">{data.supervisorLogPath}</dd>
              </div>
            </dl>
            <p className="text-sm text-fg-muted">
              {copy(
                "Disabling or removing startup leaves the current gateway running. Credentials remain under your account; this does not run before Windows login.",
                "禁用或移除启动注册不会停止当前网关。凭据仍由当前账号使用；此方式在登录 Windows 后运行。",
              )}
            </p>
          </Panel>
          <Panel title={copy("Command-line recovery", "命令行恢复")}>
            {Object.entries(data.commands).map(([action, command]) => (
              <CommandRow
                key={action}
                label={action}
                command={command}
                copied={copied === command}
                onCopy={() => {
                  void navigator.clipboard
                    .writeText(command)
                    .then(() => setCopied(command))
                    .catch(() =>
                      setNotice({
                        error: true,
                        text: copy(
                          "Copy failed; select the command manually.",
                          "复制失败，请手动选择命令。",
                        ),
                      }),
                    );
                }}
              />
            ))}
          </Panel>
        </>
      )}
    </div>
  );
}
