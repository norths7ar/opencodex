import CompactionRoutingPanel from "../components/CompactionRoutingPanel";
import ReasoningRetentionPanel from "../components/ReasoningRetentionPanel";
import MemoryObservabilityCard from "../components/MemoryObservabilityCard";
import MemoryModelsPanel from "../components/MemoryModelsPanel";
import NativeReasoningRetentionPanel from "../components/NativeReasoningRetentionPanel";
import FallbackPanel from "../components/FallbackPanel";
import SimpleFallbackPanel from "../components/SimpleFallbackPanel";
import WorkflowBudgetPanel from "../components/WorkflowBudgetPanel";
import type { useDashboardData } from "./use-dashboard-data";
import {
  DashboardCodexSection,
  DashboardSidecarPanels,
  DashboardSubagentSection,
} from "./dashboard-overview-sections";
import { SettingsSection } from "@/components/v2-proxy/settings-layout";

type Dash = ReturnType<typeof useDashboardData>;

export function DashboardOverviewPanels({
  readOnly = false,
  ...props
}: Dash & { readOnly?: boolean }) {
  const { t } = props;
  if (props.liteRuntime)
    return (
      <>
        <DashboardSubagentSection d={props} />
        <DashboardCodexSection d={props} compactionOnly />
        <DashboardSidecarPanels d={props} hideShadow />
        <SettingsSection
          id="proxy-compaction"
          title={t("proxyV2.secCompaction")}
          lede={t("proxyV2.secCompactionLede")}
        >
          <CompactionRoutingPanel
            apiBase={props.apiBase}
            models={props.models}
            providers={props.providers}
          />
        </SettingsSection>
        <WorkflowBudgetPanel apiBase={props.apiBase} id="proxy-budget" />
        <SimpleFallbackPanel apiBase={props.apiBase} readOnly={readOnly} />
      </>
    );
  return (
    <>
      <DashboardSubagentSection d={props} />
      <DashboardCodexSection d={props} />
      <DashboardSidecarPanels d={props} />
      {/* The panels below own their own load/save; the section frame only groups them. */}
      <SettingsSection
        id="proxy-compaction"
        title={t("proxyV2.secCompaction")}
        lede={t("proxyV2.secCompactionLede")}
      >
        <CompactionRoutingPanel
          apiBase={props.apiBase}
          models={props.models}
          providers={props.providers}
        />
        <NativeReasoningRetentionPanel apiBase={props.apiBase} />
        <ReasoningRetentionPanel apiBase={props.apiBase} />
      </SettingsSection>
      <SettingsSection
        id="proxy-memory"
        title={t("proxyV2.secMemory")}
        lede={t("proxyV2.secMemoryLede")}
      >
        <MemoryModelsPanel apiBase={props.apiBase} models={props.models} />
        <MemoryObservabilityCard apiBase={props.apiBase} />
      </SettingsSection>
      <SettingsSection
        id="proxy-fallback"
        title={t("proxyV2.secFallback")}
        lede={t("proxyV2.secFallbackLede")}
      >
        <FallbackPanel apiBase={props.apiBase} readOnly={readOnly} embedded />
      </SettingsSection>
    </>
  );
}
