import { DashboardOverviewHead } from "./dashboard-overview-head";
import { DashboardOverviewPanels } from "./dashboard-overview-panels";
import FallbackPanel from "../components/FallbackPanel";
import type { useDashboardData } from "./use-dashboard-data";

type Dash = ReturnType<typeof useDashboardData>;

export function DashboardOverviewSection({ readOnly = false, ...d }: Dash & { readOnly?: boolean }) {
  return (
    <div className="dash-overview-stack">
      <DashboardOverviewHead {...d} />
      <DashboardOverviewPanels {...d} />
      <FallbackPanel apiBase={d.apiBase} readOnly={readOnly} />
    </div>
  );
}
