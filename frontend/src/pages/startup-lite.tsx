import { Notice } from "@/ui";
import { CommandRow } from "@/components/command-row";
import { FileLocation } from "@/components/file-location";
import { StateBadge } from "@/components/state-badge";
import { useEffect, useState } from "react";
import { DataSurfaceSkeleton } from "../components/data-surface";
import { Button } from "../components/button";
import type { DataSurfaceResource } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { useI18n } from "../i18n/shared";
import { IconActivity, IconPower, IconRefresh } from "../icons";
import type { Overview } from "./use-overview";
import "./startup-lite.css";

export default function LiteStartup({
  apiBase,
  overview: resource,
  onReloadCodex,
  reloadingCodex,
}: {
  apiBase: string;
  overview: DataSurfaceResource<Overview>;
  onReloadCodex: () => void;
  reloadingCodex: boolean;
}) {
  const { t } = useI18n();
  const unconfirmed = t("startup.surface.unconfirmed");
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<{ id: string; action: string } | null>(null);
  const [notice, setNotice] = useState<{
    error: boolean;
    text: string;
    removalDisconnected?: boolean;
  } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const openLocation = async (location: "state" | "runtime-log" | "startup-log") => {
    setOpening(location);
    try {
      await fetch(`${apiBase}/api/runtime/open-location`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location }),
      }).then(readJsonOrThrow);
    } catch (error) {
      setNotice({
        error: true,
        text: error instanceof Error ? error.message : t("startup.surface.openFolderFailed"),
      });
    } finally {
      setOpening(null);
    }
  };
  const copyValue = (value: string) => {
    void navigator.clipboard
      .writeText(value)
      .then(() => setCopied(value))
      .catch(() =>
        setNotice({
          error: true,
          text: t("startup.surface.copyFailed"),
        }),
      );
  };
  const data = resource.state.data?.startup;
  const runtime = resource.state.data?.runtime;
  const observed = data?.serviceOperation;
  // Consume the receipt as soon as it matches the tracked operation. A later
  // overview can describe a different operation and must not restart this poll.
  if (operation && observed?.id === operation.id && observed.state !== "running") {
    const failed = observed.state === "failed";
    setOperation(null);
    setNotice({
      error: failed,
      text:
        observed.error ??
        (failed ? t("startup.surface.applyFailed") : t("startup.surface.serviceUpdated")),
    });
  } else if (operation?.action === "remove" && resource.state.showError) {
    setOperation(null);
    setNotice({
      error: false,
      removalDisconnected: true,
      text: t("startup.surface.removalDisconnected"),
    });
  }
  const removalDisconnected = notice?.removalDisconnected && resource.state.showError;
  const removing = operation?.action === "remove";
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
    const interval = setInterval(() => refresh(), 1000);
    const timeout = setTimeout(() => {
      setOperation(null);
      setNotice({
        error: true,
        text: unconfirmed,
      });
    }, 90_000);
    return () => {
      clearInterval(interval);
      clearTimeout(timeout);
    };
  }, [operation, refresh, unconfirmed]);

  const act = async (action: string) => {
    setBusy(true);
    setOperation(null);
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
      }>(response, t("startup.surface.applyFailed"));
      if (result?.pending && result.operationId) {
        setOperation({ id: result.operationId, action: result.action ?? action });
        setNotice({
          error: false,
          text:
            action === "remove"
              ? t("startup.surface.removalPending")
              : action === "repair"
                ? t("startup.surface.repairPending")
                : t("startup.surface.installPending"),
        });
        return;
      }
      setNotice({ error: false, text: t("startup.surface.startupUpdated") });
      resource.refresh();
    } catch (error) {
      setNotice({ error: true, text: error instanceof Error ? error.message : String(error) });
      resource.refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="startup-page">
      {resource.state.showError && <Notice tone="err">{t("startup.surface.readFailed")}</Notice>}
      {!data && !resource.state.showError && (
        <DataSurfaceSkeleton label={t("startup.surface.loading")} rows={3} />
      )}
      {notice && <Notice tone={notice.error ? "err" : "ok"}>{notice.text}</Notice>}
      {data && (
        <>
          {data.error && <Notice tone="warn">{data.error}</Notice>}
          <div className="startup-control">
            <section className="startup-runtime-status">
              <div>
                <div className="startup-control-heading">
                  <h2>
                    <IconActivity aria-hidden="true" />
                    {t("startup.surface.gateway")}
                  </h2>
                  <StateBadge
                    tone={
                      !removalDisconnected && !removing && data.runtimeState === "running"
                        ? "ok"
                        : "neutral"
                    }
                  >
                    {removalDisconnected
                      ? t("startup.surface.disconnected")
                      : removing
                        ? t("startup.surface.stopping")
                        : data.runtimeState === "running"
                          ? t("startup.surface.running")
                          : data.runtimeState}
                  </StateBadge>
                </div>
                <p>{t("startup.surface.gatewayDescription")}</p>
              </div>
              <dl className="startup-runtime-facts">
                <div>
                  <dt>{t("startup.surface.port")}</dt>
                  <dd>{runtime?.port ?? "—"}</dd>
                </div>
                <div>
                  <dt>{t("shellV2.tileUptime")}</dt>
                  <dd>
                    {runtime?.uptimeSeconds === undefined
                      ? "—"
                      : t("shellV2.uptimeValue", {
                          h: String(Math.floor(runtime.uptimeSeconds / 3600)),
                          m: String(Math.floor((runtime.uptimeSeconds % 3600) / 60)),
                        })}
                  </dd>
                </div>
                <div>
                  <dt>{t("shellV2.tileVersion")}</dt>
                  <dd>{runtime?.version ? `v${runtime.version}` : "—"}</dd>
                </div>
              </dl>
            </section>
            <section className="startup-auto">
              <div>
                <div className="startup-control-heading">
                  <h2>
                    <IconPower aria-hidden="true" />
                    {t("startup.surface.automatic")}
                  </h2>
                  <StateBadge
                    tone={
                      operation || removalDisconnected || data.diagnosticStale
                        ? "neutral"
                        : data.serviceViable
                          ? "ok"
                          : data.serviceStale
                            ? "warn"
                            : "neutral"
                    }
                  >
                    {removalDisconnected
                      ? t("startup.surface.checkCli")
                      : removing
                        ? t("startup.surface.removing")
                        : data.diagnosticStale
                          ? t("startup.surface.statusUnavailable")
                          : data.serviceViable
                            ? t("startup.surface.enabled")
                            : data.serviceStale
                              ? t("startup.surface.repairNeeded")
                              : t("startup.surface.notEnabled")}
                  </StateBadge>
                </div>
                <p>{t("startup.surface.automaticDescription")}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {!data.serviceInstalled && (
                  <Button variant="primary" disabled={blocked} onClick={() => void act("install")}>
                    {t("startup.surface.install")}
                  </Button>
                )}
                {data.serviceInstalled && (
                  <>
                    <Button
                      disabled={blocked}
                      onClick={() => void act(data.serviceEnabled ? "disable" : "enable")}
                    >
                      {data.serviceEnabled
                        ? t("startup.surface.disable")
                        : t("startup.surface.enable")}
                    </Button>
                    <Button disabled={blocked} onClick={() => void act("repair")}>
                      {t("startup.surface.repair")}
                    </Button>
                  </>
                )}
              </div>
            </section>
            <div className="startup-settings-body">
              <details className="startup-details">
                <summary>{t("startup.surface.runtimeDetails")}</summary>
                <div className="startup-details-content">
                  <p className="startup-action-hint">{t("startup.surface.actionHint")}</p>
                  <div className="startup-detail-list">
                    <FileLocation
                      label={t("startup.surface.localData")}
                      name={t("startup.surface.dataDirectory")}
                      openLabel={t("startup.surface.openFolder")}
                      busy={opening === "state"}
                      onOpen={() => void openLocation("state")}
                    />
                    <FileLocation
                      label={t("startup.surface.runtimeLog")}
                      name={data.logPath.split(/[\\/]/).pop() || "runtime.log"}
                      openLabel={t("startup.surface.openFolder")}
                      busy={opening === "runtime-log"}
                      onOpen={() => void openLocation("runtime-log")}
                    />
                    <FileLocation
                      label={t("startup.surface.startupLog")}
                      name={data.supervisorLogPath.split(/[\\/]/).pop() || "startup.log"}
                      openLabel={t("startup.surface.openFolder")}
                      busy={opening === "startup-log"}
                      onOpen={() => void openLocation("startup-log")}
                    />
                  </div>
                  <p className="startup-action-hint">
                    {t("startup.surface.startupTask")}: {data.taskName}
                  </p>
                  <div className="startup-detail-actions">
                    <Button
                      size="sm"
                      onClick={onReloadCodex}
                      disabled={reloadingCodex}
                      aria-busy={reloadingCodex}
                    >
                      <IconRefresh
                        className={reloadingCodex ? "motion-safe:animate-spin" : undefined}
                        aria-hidden="true"
                      />
                      {t(reloadingCodex ? "dash.codexRestarting" : "dash.codexRestart")}
                    </Button>
                    {data.serviceInstalled && (
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={blocked}
                        onClick={() => void act("remove")}
                      >
                        {t("startup.surface.remove")}
                      </Button>
                    )}
                  </div>
                </div>
              </details>
              {Object.keys(data.commands).length > 0 && (
                <details className="startup-recovery">
                  <summary>{t("startup.surface.recovery")}</summary>
                  <p>{t("startup.surface.recoveryHint")}</p>
                  {Object.entries(data.commands).map(([action, command]) => (
                    <CommandRow
                      key={action}
                      label={action}
                      command={command}
                      copied={copied === command}
                      onCopy={() => copyValue(command)}
                    />
                  ))}
                </details>
              )}
            </div>
          </div>
        </>
      )}
      {!data && (
        <div className="startup-detail-actions">
          <Button size="sm" onClick={onReloadCodex} disabled={reloadingCodex}>
            {t(reloadingCodex ? "dash.codexRestarting" : "dash.codexRestart")}
          </Button>
        </div>
      )}
    </div>
  );
}
