import { useCallback, useEffect, useState } from "react";
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
import "./startup-lite.css";

interface StartupHealth {
  serviceOperation?: { id: string; state: string; error?: string } | null;
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
  const [operation, setOperation] = useState<{ id: string; action: string } | null>(null);
  const [removalDisconnected, setRemovalDisconnected] = useState(false);
  const removing = operation?.action === "remove";
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
    busy ||
    !!operation ||
    resource.state.refreshing ||
    !data ||
    data.diagnosticStale ||
    data.serviceConflict;
  const refresh = resource.refresh;

  useEffect(() => {
    if (!operation) return;
    const controller = new AbortController();
    const deadline = Date.now() + 90_000;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await load(controller.signal);
        const observed = result?.serviceOperation;
        if (observed?.id === operation.id && observed.state !== "running") {
          setOperation(null);
          setNotice({
            error: observed.state === "failed",
            text: observed.error ?? (zh ? "服务设置已更新。" : "Service settings updated."),
          });
          refresh();
          return;
        }
      } catch {
        if (controller.signal.aborted) return;
        if (operation.action === "remove") {
          setRemovalDisconnected(true);
          setNotice({
            error: false,
            text: zh
              ? "网关连接已断开。运行 ocx service status 查看移除结果。"
              : "The gateway connection has closed. Run ocx service status to check the removal result.",
          });
          return;
        }
        // Installation and repair can briefly stop the server hosting this page.
      }
      if (controller.signal.aborted) return;
      if (Date.now() >= deadline) {
        setOperation(null);
        setNotice({
          error: true,
          text: zh
            ? "暂时无法确认结果，请运行 ocx service status。"
            : "Could not confirm the result. Run ocx service status.",
        });
        return;
      }
      timer = setTimeout(() => void poll(), 1000);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [operation, load, refresh, zh]);

  const act = async (action: string) => {
    setBusy(true);
    setNotice(null);
    try {
      const response = await fetch(`${apiBase}/api/startup-action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const result = await readJsonOrThrow<{
        pending?: boolean;
        operationId?: string;
        action?: string;
      }>(response, copy("Could not change automatic startup.", "无法更改自动启动设置。"));
      if (result?.pending && result.operationId) {
        setOperation({ id: result.operationId, action: result.action ?? action });
        setNotice({
          error: false,
          text:
            action === "remove"
              ? copy("Stopping the gateway and removing the service…", "正在停止网关并移除服务……")
              : action === "repair"
                ? copy("Repairing the service…", "正在修复服务……")
                : copy("Installing the service…", "正在安装服务……"),
        });
        return;
      }
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
          <Button
            disabled={busy || !!operation || resource.state.refreshing}
            onClick={() => resource.refresh()}
          >
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
          <section className="startup-runtime-status">
            <div>
              <h3>{copy("Gateway", "网关运行状态")}</h3>
              <p>
                {copy(
                  "Closing this console leaves the gateway running in the background.",
                  "关闭控制台后，网关继续在后台运行。",
                )}
              </p>
            </div>
            <StateBadge tone={!removing && data.runtimeState === "running" ? "ok" : "neutral"}>
              {removalDisconnected
                ? copy("Disconnected", "连接已断开")
                : removing
                  ? copy("Stopping", "正在停止")
                  : data.runtimeState === "running"
                    ? copy("Running", "运行中")
                    : data.runtimeState}
            </StateBadge>
          </section>
          <Panel
            title={copy("Automatic background startup", "自动后台启动")}
            meta={
              <StateBadge
                tone={
                  operation
                    ? "neutral"
                    : data.serviceViable
                      ? "ok"
                      : data.serviceStale
                        ? "warn"
                        : "neutral"
                }
              >
                {removalDisconnected
                  ? copy("Check from CLI", "请用命令行确认")
                  : removing
                    ? copy("Removing", "正在移除")
                    : data.diagnosticStale
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
            bodyClassName="startup-settings-body"
          >
            <p className="startup-action-hint">
              {copy(
                "Disabling automatic startup leaves the gateway running. Removing the service stops it.",
                "禁用自动启动保留网关运行；移除服务会停止网关。",
              )}
            </p>
            <details className="startup-details">
              <summary>{copy("Runtime details", "运行详情")}</summary>
              <dl className="startup-detail-list">
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
            </details>
          </Panel>
          <details className="startup-recovery">
            <summary>{copy("Command-line recovery", "命令行恢复")}</summary>
            <p>
              {copy(
                "Use these commands if the console is unavailable or a startup task needs repair.",
                "控制台无法访问或启动任务需要修复时，可使用以下命令。",
              )}
            </p>
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
          </details>
        </>
      )}
    </div>
  );
}
