import { PageHeader } from "../components/page-header";
import { classifyDataSurface, useDataSurface } from "../data-surface";
import { useT, getActiveLocale } from "../i18n/shared";
import type { Page } from "../app-routing";
import { IconRefresh } from "../icons";
import { Button } from "@/components/ui-v2/button";
import { cn } from "@/lib/utils";
import { recentFailures, type HomeLogRow } from "./home-failures";
import { startupChecklistDone } from "../startup-health-ui";
import { useStartupChecklist } from "../hooks/useStartupChecklist";
import { useRuntimeCapabilities } from "../runtime-capabilities";

/*
 * Design system v2 landing page. It answers the two questions people open the dashboard
 * with — "is everything working?" and "what do I do next?" — and nothing else: settings
 * live in their own tier. Every figure comes from an endpoint another page already reads;
 * Home adds no API.
 */

interface HomeProps {
  apiBase: string;
  navigate: (page: Page, subPath?: string) => void;
  onReloadCodex: () => void;
  reloadingCodex: boolean;
}

interface Health {
  version?: string;
  uptime?: number;
  uptimeSeconds?: number;
}
interface UsageDay {
  requests?: number;
  totalTokens?: number;
}
interface UsageBody {
  summary?: { requests?: number; totalTokens?: number };
  days?: UsageDay[];
}
interface ConfigBody {
  providers?: Record<string, unknown>;
}
interface LogRow extends HomeLogRow {
  model?: string;
  provider?: string;
  errorCode?: string;
  upstreamError?: string;
}

const DAY_MS = 24 * 60 * 60 * 1000;

async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body: unknown = await res.json();
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Invalid response");
  return body as T;
}

export default function Home({ apiBase, navigate, onReloadCodex, reloadingCodex }: HomeProps) {
  const t = useT();
  const capabilities = useRuntimeCapabilities();
  const locale = getActiveLocale();
  const number = new Intl.NumberFormat(locale);
  const compact = new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 });

  const { state: health, refresh: refreshHealth } = useDataSurface(
    `home-health:${apiBase}`,
    [apiBase],
    (signal) => getJson<Health>(`${apiBase}/healthz`, signal),
    { isEmpty: () => false, pollMs: 30_000 },
  );
  const { state: usage, refresh: refreshUsage } = useDataSurface(
    `home-usage:${apiBase}`,
    [apiBase],
    (signal) => getJson<UsageBody>(`${apiBase}/api/usage?range=7d`, signal),
    { isEmpty: () => false, pollMs: 60_000 },
  );
  const { state: config, refresh: refreshConfig } = useDataSurface(
    `home-config:${apiBase}`,
    [apiBase],
    (signal) => getJson<ConfigBody>(`${apiBase}/api/config`, signal),
    { isEmpty: () => false },
  );
  const startupResource = useStartupChecklist(apiBase);
  const startup = classifyDataSurface(startupResource, () => false, true);
  // The 24-hour window is cut when the rows arrive, so render never reads the clock.
  const { state: failureRead, refresh: refreshFailureRead } = useDataSurface(
    `home-failures:${apiBase}`,
    [apiBase],
    async (signal) => {
      const response = await fetch(`${apiBase}/api/logs?limit=500`, { signal });
      if (!response.ok) throw new Error("logs unavailable");
      const body: unknown = await response.json();
      if (!body || typeof body !== "object" || !("logs" in body) || !Array.isArray(body.logs))
        throw new Error("logs unavailable");
      return recentFailures(body.logs as LogRow[], Date.now() - DAY_MS);
    },
    { isEmpty: (rows) => rows.length === 0, pollMs: 30_000 },
  );
  const failures = failureRead.data ?? [];

  const providerCount = config.data ? Object.keys(config.data.providers ?? {}).length : undefined;
  const requests7d = usage.data?.summary?.requests;
  const tokens7d = usage.data?.summary?.totalTokens;
  const uptime = health.data?.uptimeSeconds ?? health.data?.uptime;

  const steps = [
    {
      key: "provider",
      done: providerCount === undefined ? undefined : providerCount > 0,
      title: t("shellV2.stepProvider"),
      hint: t("shellV2.stepProviderHint"),
      go: () => navigate("providers"),
    },
    {
      key: "request",
      done: requests7d === undefined ? undefined : requests7d > 0,
      title: t("shellV2.stepFirstRequest"),
      hint: t("shellV2.stepFirstRequestHint"),
      go: () => navigate("integrations"),
    },
    {
      key: "autostart",
      done: startup.data ? startupChecklistDone(startup.data) : undefined,
      title: t("shellV2.stepAutostart"),
      hint: t("shellV2.stepAutostartHint"),
      go: () => navigate("startup"),
    },
  ];
  const doneCount = steps.filter((step) => step.done).length;
  const progressKnown = steps.every((step) => step.done !== undefined);
  const errors = [
    health.error != null && t("shellV2.homeDown"),
    usage.error != null && t("usage.loadError"),
    config.error != null && t("prov.loadConfigFail"),
    startup.error != null && t("startup.error"),
    failureRead.error != null && t("shellV2.homeErrorsUnavailable"),
  ].filter(Boolean);

  const headline = errors.length
    ? errors.join(" · ")
    : health.showSkeleton || failureRead.showSkeleton
      ? "\u00a0"
      : failures.length > 0
        ? t("shellV2.homeErrorsToday", { count: number.format(failures.length) })
        : t("shellV2.homeOk");
  const retry = () => {
    refreshHealth();
    refreshUsage();
    refreshConfig();
    refreshFailureRead();
    startupResource.refresh();
  };

  return (
    <div className="ui-v2 flex flex-col gap-6 font-sans text-fg">
      <PageHeader
        title={t("shellV2.navHome")}
        subtitle={<span role={errors.length ? "alert" : undefined}>{headline}</span>}
        actions={
          <>
            {errors.length > 0 && <Button onClick={retry}>{t("common.retry")}</Button>}
            <Button variant="outline" onClick={onReloadCodex} disabled={reloadingCodex}>
              <span className={cn("inline-flex", reloadingCodex && "animate-spin")}>
                <IconRefresh />
              </span>
              {t(reloadingCodex ? "dash.codexRestarting" : "dash.codexRestart")}
            </Button>
            <Button variant="primary" onClick={() => navigate("logs")}>
              {t("shellV2.openLogs")}
            </Button>
          </>
        }
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label={t("shellV2.tileVersion")}
          value={health.data?.version ? `v${health.data.version}` : "—"}
        />
        <Stat
          label={t("shellV2.tileUptime")}
          value={
            uptime !== undefined
              ? t("shellV2.uptimeValue", {
                  h: String(Math.floor(uptime / 3600)),
                  m: String(Math.floor((uptime % 3600) / 60)),
                })
              : "—"
          }
        />
        <Stat
          label={t("shellV2.tileRequests")}
          value={requests7d === undefined ? "—" : number.format(requests7d)}
        />
        <Stat
          label={t("shellV2.tileTokens")}
          value={tokens7d === undefined ? "—" : compact.format(tokens7d)}
        />
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <section className="flex flex-col gap-4 rounded-card bg-surface p-5">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="m-0 text-[15px] font-semibold">{t("shellV2.checklistTitle")}</h2>
            {
              <span
                className="text-[13px] text-fg-muted tabular-nums"
                style={{ visibility: progressKnown ? "visible" : "hidden" }}
              >
                {t("shellV2.checklistProgress", {
                  done: String(doneCount),
                  total: String(steps.length),
                })}
              </span>
            }
          </div>
          {
            <div className="h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden>
              <div
                className="h-full origin-left rounded-full bg-accent transition-transform duration-[600ms] ease-soft"
                style={{
                  visibility: progressKnown ? "visible" : "hidden",
                  transform: `scaleX(${doneCount / steps.length})`,
                }}
              />
            </div>
          }
          {
            <ol className="m-0 flex list-none flex-col gap-1 p-0">
              {steps.map((step, index) => {
                const current =
                  step.done === false && steps.findIndex((s) => s.done === false) === index;
                return (
                  <li key={step.key}>
                    <button
                      type="button"
                      onClick={step.go}
                      className={cn(
                        "group flex w-full cursor-pointer items-center gap-3 rounded-control p-2.5 text-left transition-colors hover:bg-raised",
                        current && "bg-raised shadow-sm ring-1 ring-line",
                      )}
                    >
                      <span
                        className={cn(
                          "grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold",
                          step.done
                            ? "bg-success text-on-accent"
                            : current
                              ? "bg-accent text-on-accent"
                              : "bg-surface-2 text-fg-muted",
                        )}
                        aria-hidden
                      >
                        {step.done ? "✓" : index + 1}
                      </span>
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span
                          className={cn(
                            "text-[14px] font-medium",
                            step.done && "text-fg-subtle line-through",
                          )}
                        >
                          {step.title}
                        </span>
                        <span
                          className="text-[13px] text-fg-muted"
                          style={{ visibility: step.done === false ? "visible" : "hidden" }}
                        >
                          {step.hint}
                        </span>
                      </span>
                      {
                        <span
                          className="text-[13px] font-medium text-highlight opacity-0 transition-opacity group-hover:opacity-100"
                          style={{ visibility: step.done === false ? "visible" : "hidden" }}
                        >
                          {t("shellV2.stepOpen")} →
                        </span>
                      }
                    </button>
                  </li>
                );
              })}
            </ol>
          }
        </section>

        <section className="flex flex-col gap-3 rounded-card bg-surface p-5">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="m-0 text-[15px] font-semibold">{t("shellV2.recentErrors")}</h2>
            <button
              type="button"
              onClick={() => navigate("logs")}
              className="cursor-pointer text-[13px] font-medium text-highlight hover:underline"
            >
              {t("shellV2.viewAll")}
            </button>
          </div>
          {failureRead.showSkeleton ? (
            <div className="min-h-5" aria-busy="true" />
          ) : failures.length === 0 ? (
            !failureRead.showError && (
              <p className="m-0 text-[14px] text-fg-muted">{t("shellV2.noErrors")}</p>
            )
          ) : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {failures.slice(0, 4).map((row) => (
                <li
                  key={`${row.timestamp}:${row.model}`}
                  className="flex items-baseline gap-2.5 rounded-control px-1 py-1.5 text-[13px]"
                >
                  <span className="font-mono text-xs text-fg-subtle tabular-nums">
                    {new Date(row.timestamp).toLocaleTimeString(locale, {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                  <span className="rounded-full bg-warning-soft px-1.5 text-[11px] font-semibold text-warning tabular-nums">
                    {row.status}
                  </span>
                  <span className="min-w-0 flex-1 truncate">
                    {row.provider} · {row.model}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-auto flex items-center justify-between gap-3 border-t border-line pt-3 text-[13px] text-fg-muted">
            <span>
              {t("shellV2.tileProviders")}{" "}
              <b className="font-semibold text-fg tabular-nums">
                {providerCount === undefined ? "—" : number.format(providerCount)}
              </b>
            </span>
            {capabilities?.runtime !== "python" && (
              <button
                type="button"
                onClick={() => navigate("dashboard", "update")}
                className="cursor-pointer font-medium text-highlight hover:underline"
              >
                {t("shellV2.checkUpdate")}
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-card bg-surface px-4 py-3.5">
      <span className="text-xs font-medium text-fg-subtle">{label}</span>
      <span className="truncate text-stat font-semibold tracking-[-0.02em] tabular-nums">
        {value}
      </span>
    </div>
  );
}
