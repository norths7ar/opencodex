import { Button } from "@/components/button";
import { cn } from "@/lib/utils";
import type { Page } from "../app-routing";
import { PageHeader } from "../components/page-header";
import { useDataSurface } from "../data-surface";
import { useI18n } from "../i18n/shared";
import { IconCheck, IconChevron, IconList, IconRefresh } from "../icons";
import { recentFailures, type HomeLogRow } from "./home-failures";
import LiteStartup from "./startup-lite";
import { useOverview } from "./use-overview";

interface HomeProps {
  apiBase: string;
  navigate: (page: Page, subPath?: string) => void;
  onReloadCodex: () => void;
  reloadingCodex: boolean;
}

interface UsageDay {
  requests?: number;
  totalTokens?: number;
}
interface UsageBody {
  summary?: { requests?: number; totalTokens?: number };
  days?: UsageDay[];
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
  const { t, locale } = useI18n();
  const number = new Intl.NumberFormat(locale);
  const compact = new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 });

  const overview = useOverview(apiBase);
  const { state: config, refreshDiagnostics } = overview;
  const { state: usage, refresh: refreshUsage } = useDataSurface(
    `home-usage:${apiBase}`,
    [apiBase],
    (signal) => getJson<UsageBody>(`${apiBase}/api/usage?range=7d`, signal),
    { isEmpty: () => false, pollMs: 60_000 },
  );
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

  const providerCount = config.data ? config.data.channels.total : undefined;
  const requests7d = usage.data?.summary?.requests;
  const tokens7d = usage.data?.summary?.totalTokens;

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
      done: config.data ? config.data.milestones.first_success_at !== null : undefined,
      title: t("shellV2.stepFirstRequest"),
      hint: t("shellV2.stepFirstRequestHint"),
      go: () => navigate("integrations"),
    },
  ];
  const doneCount = steps.filter((step) => step.done).length;
  const progressKnown = steps.every((step) => step.done !== undefined);
  const errors = [
    config.error != null && t("shellV2.homeDown"),
    usage.error != null && t("usage.loadError"),
    failureRead.error != null && t("shellV2.homeErrorsUnavailable"),
  ].filter(Boolean);

  const retry = () => {
    refreshUsage();
    refreshDiagnostics();
    refreshFailureRead();
  };

  return (
    <div className="home-console ui-v2 font-sans text-fg">
      <PageHeader
        title={t("shellV2.navHome")}
        actions={
          <Button
            onClick={retry}
            disabled={config.refreshing || usage.refreshing || failureRead.refreshing}
          >
            <IconRefresh aria-hidden="true" />
            {t(errors.length > 0 ? "common.retry" : "startup.refresh")}
          </Button>
        }
      />

      <LiteStartup
        apiBase={apiBase}
        overview={overview}
        onReloadCodex={onReloadCodex}
        reloadingCodex={reloadingCodex}
      />

      <dl className="home-stat-strip">
        <Stat
          label={t("shellV2.tileProviders")}
          value={providerCount === undefined ? "—" : number.format(providerCount)}
        />
        <Stat
          label={t("shellV2.tileRequests")}
          value={requests7d === undefined ? "—" : number.format(requests7d)}
        />
        <Stat
          label={t("shellV2.tileTokens")}
          value={tokens7d === undefined ? "—" : compact.format(tokens7d)}
        />
      </dl>
      {usage.showError && (
        <p className="home-inline-warning" role="status">
          {t("usage.loadError")}
        </p>
      )}

      <div className="home-secondary">
        {(!progressKnown || doneCount < steps.length) && (
          <section className="home-support-panel">
            <div className="home-section-heading">
              <h2>{t("shellV2.checklistTitle")}</h2>
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
              <ol className="home-setup-steps">
                {steps.map((step, index) => {
                  const current =
                    step.done === false && steps.findIndex((s) => s.done === false) === index;
                  return (
                    <li key={step.key}>
                      <button
                        type="button"
                        onClick={step.go}
                        className={cn("home-setup-step", current && "home-setup-step--current")}
                      >
                        <span
                          className={cn(
                            "grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold",
                            step.done
                              ? "bg-success-soft text-success"
                              : current
                                ? "bg-accent text-on-accent"
                                : "bg-surface-2 text-fg-muted",
                          )}
                          aria-hidden
                        >
                          {step.done ? <IconCheck width={14} height={14} /> : index + 1}
                        </span>
                        <span className="flex min-w-0 flex-1 flex-col">
                          <span
                            className={cn("text-[14px] font-medium", step.done && "text-fg-muted")}
                          >
                            {step.title}
                          </span>
                          {step.done === false && (
                            <span className="text-xs text-fg-muted">{step.hint}</span>
                          )}
                        </span>
                        <IconChevron
                          width={15}
                          height={15}
                          className="shrink-0 text-accent"
                          aria-hidden="true"
                        />
                      </button>
                    </li>
                  );
                })}
              </ol>
            }
          </section>
        )}

        <section className="home-support-panel">
          <div className="home-section-heading">
            <h2>{t("shellV2.recentErrors")}</h2>
            <Button size="sm" variant="ghost" onClick={() => navigate("logs")}>
              <IconList aria-hidden="true" />
              {t("shellV2.openLogs")}
            </Button>
          </div>
          {failureRead.showError && (
            <p className="home-inline-warning" role="status">
              {t("shellV2.homeErrorsUnavailable")}
            </p>
          )}
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
        </section>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
