import { type ReactNode } from "react";
import { PageHeader } from "../components/page-header";
import { DataSurfaceSkeleton } from "../components/data-surface";
import { IconAlert } from "../icons";
import { Trans } from "../i18n/provider";
import { navigateHash } from "../hash-routing";
import { EmptyState, Notice } from "../ui";
import { DashboardModelsSection } from "./dashboard-models-section";
import { DashboardOverviewSection } from "./dashboard-overview-section";
import { DashboardProvidersSection } from "./dashboard-providers-section";
import { dashboardHashForSection, type DashboardSection } from "./dashboard-shared";
import { useDashboardData } from "./use-dashboard-data";

function selectDashboardTab(next: DashboardSection) {
  // Deliberate navigation: push a history entry so Back/Forward restore the tab.
  navigateHash(dashboardHashForSection(next));
}

export default function Dashboard({
  apiBase,
  readOnly = false,
  authenticationPending = false,
  refreshEpoch = 0,
}: {
  apiBase: string;
  readOnly?: boolean;
  authenticationPending?: boolean;
  refreshEpoch?: number;
}) {
  const d = useDashboardData(apiBase, refreshEpoch);
  const {
    t,
    error,
    selectedSection,
    providers,
    models,
    modelQuery,
    setModelQuery,
    filteredGroups,
    expandedProviders,
    setExpandedProviders,
  } = d;

  if (authenticationPending) return null;
  const unavailable = (
    <EmptyState
      style={{ marginTop: 40 }}
      icon={<IconAlert />}
      title={
        <span style={{ color: "var(--red)" }}>
          {t(
            d.connectionFailure === "denied"
              ? "dash.permissionDenied"
              : d.connectionFailure === "auth"
                ? "dash.authRequired"
                : "dash.dataUnavailable",
          )}
        </span>
      }
    >
      {d.connectionFailure === "unavailable" && <Trans k="dash.runStart" cmd="ocx start" />}
      <button className="btn btn-ghost" type="button" onClick={() => d.refreshDashboard()}>
        {t("common.retry")}
      </button>
    </EmptyState>
  );

  const skeleton = <DataSurfaceSkeleton label={t("common.loading")} rows={5} />;
  const overviewSection = <DashboardOverviewSection {...d} readOnly={readOnly} />;
  const providersSection = d.healthLoading ? (
    skeleton
  ) : (
    <DashboardProvidersSection t={t} providers={providers} />
  );
  const modelsSection = d.modelsSurface.showSkeleton ? (
    skeleton
  ) : (
    <>
      {d.modelsSurface.showError && (
        <Notice tone="warn">
          {t(d.modelsSurface.data ? "dash.staleData" : "dash.dataUnavailable")}{" "}
          <button className="btn btn-ghost" type="button" onClick={() => d.refreshModels()}>
            {t("common.retry")}
          </button>
        </Notice>
      )}
      {d.modelsSurface.data && (
        <DashboardModelsSection
          t={t}
          models={models}
          modelsLoading={false}
          modelQuery={modelQuery}
          setModelQuery={setModelQuery}
          filteredGroups={filteredGroups}
          expandedProviders={expandedProviders}
          setExpandedProviders={setExpandedProviders}
        />
      )}
    </>
  );

  const sections: { id: DashboardSection; label: string; body: ReactNode }[] = [
    { id: "overview", label: t("dash.workspace.overview"), body: overviewSection },
    { id: "providers", label: t("dash.activeProviders"), body: providersSection },
    { id: "models", label: t("dash.availableModels"), body: modelsSection },
  ];
  const selected = sections.find((s) => s.id === selectedSection) ?? sections[0];
  const selectTab = selectDashboardTab;
  const onTabKeyDown = (e: React.KeyboardEvent) => {
    const index = sections.findIndex((s) => s.id === selectedSection);
    let next = -1;
    if (e.key === "ArrowRight") next = (index + 1) % sections.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + sections.length) % sections.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = sections.length - 1;
    if (next < 0) return;
    e.preventDefault();
    const target = sections[next]!;
    selectTab(target.id);
    document.getElementById(`dashboard-tab-${target.id}`)?.focus();
  };

  return (
    <div className="dashboard-workspace-shell">
      <PageHeader title={t("shellV2.pageProxyDetails")} subtitle={t("proxyV2.pageLede")} />
      <div className="page-tabs" role="tablist" aria-label={t("dash.workspace.sections")}>
        {sections.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            id={`dashboard-tab-${s.id}`}
            aria-selected={selectedSection === s.id}
            aria-controls={`dashboard-panel-${s.id}`}
            tabIndex={selectedSection === s.id ? 0 : -1}
            className={`page-tab${selectedSection === s.id ? " page-tab--active" : ""}`}
            onClick={() => selectTab(s.id)}
            onKeyDown={onTabKeyDown}
          >
            {s.label}
          </button>
        ))}
      </div>
      <section
        className="dashboard-workspace-main"
        role="tabpanel"
        id={`dashboard-panel-${selected.id}`}
        aria-labelledby={`dashboard-tab-${selected.id}`}
        tabIndex={0}
      >
        {!d.overviewBlocked && d.health && error && (
          <Notice tone="warn">
            {t("dash.staleData")}{" "}
            <button className="btn btn-ghost" type="button" onClick={() => d.refreshDashboard()}>
              {t("common.retry")}
            </button>
          </Notice>
        )}
        {d.overviewBlocked ? unavailable : selected.body}
      </section>
    </div>
  );
}
