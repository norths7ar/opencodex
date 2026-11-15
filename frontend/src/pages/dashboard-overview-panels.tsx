import { Fragment } from "react";
import CompactionRoutingPanel from "../components/CompactionRoutingPanel";
import ReasoningRetentionPanel from "../components/ReasoningRetentionPanel";
import MemoryObservabilityCard from "../components/MemoryObservabilityCard";
import MemoryModelsPanel from "../components/MemoryModelsPanel";
import NativeReasoningRetentionPanel from "../components/NativeReasoningRetentionPanel";
import FallbackPanel from "../components/FallbackPanel";
import SimpleFallbackPanel from "../components/SimpleFallbackPanel";
import { navigateHash } from "../hash-routing";
import type { useDashboardData } from "./use-dashboard-data";
import {
  DashboardCodexSection,
  DashboardSidecarPanels,
  DashboardSubagentSection,
} from "./dashboard-overview-sections";
import { SettingsSection } from "@/components/v2-proxy/settings-layout";
import { LITE_PROXY_SECTION_IDS } from "./dashboard-section-ids";

type Dash = ReturnType<typeof useDashboardData>;

export function DashboardOverviewPanels({
  readOnly = false,
  ...props
}: Dash & { readOnly?: boolean }) {
  const { t } = props;
  if (props.liteRuntime) {
    const panels = {
      "proxy-subagents": (
        <SettingsSection id="proxy-subagents" title={t("proxyV2.secSubagents")}>
          <div className="flex flex-wrap items-center justify-between gap-4 p-4">
            <dl className="m-0 flex flex-wrap gap-6 text-sm">
              <div>
                <dt className="muted">{t("dash.multiAgent")}</dt>
                <dd className="m-0 mt-1">
                  {props.maModeResolved
                    ? props.maMode === "default"
                      ? "base"
                      : props.maMode
                    : "—"}
                </dd>
              </div>
              <div>
                <dt className="muted">{t("dash.injectionLabel")}</dt>
                <dd className="m-0 mt-1">
                  {props.injectionReady ? props.injectionModel || t("dash.injectionNone") : "—"}
                </dd>
              </div>
            </dl>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => navigateHash("subagents?section=settings")}
            >
              {props.locale === "zh" || props.locale === "zh-TW"
                ? "管理子代理"
                : "Manage subagents"}
            </button>
          </div>
        </SettingsSection>
      ),
      "proxy-codex": <DashboardCodexSection d={props} compactionOnly />,
      "proxy-sidecars": <DashboardSidecarPanels d={props} hideShadow />,
      "proxy-compaction": (
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
      ),
      "proxy-budget": (
        <SettingsSection id="proxy-budget" title={t("workflowBudget.title")}>
          <div className="flex flex-wrap items-center justify-between gap-4 p-4">
            <span className="muted text-sm">{t("subagentsV2.budgetLede")}</span>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => navigateHash("subagents?section=budget")}
            >
              {props.locale === "zh" || props.locale === "zh-TW"
                ? "管理子代理"
                : "Manage subagents"}
            </button>
          </div>
        </SettingsSection>
      ),
      "simple-fallback": <SimpleFallbackPanel apiBase={props.apiBase} readOnly={readOnly} />,
    };
    return LITE_PROXY_SECTION_IDS.map(([id]) => <Fragment key={id}>{panels[id]}</Fragment>);
  }
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
