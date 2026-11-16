import CompactionRoutingPanel from "@/components/CompactionRoutingPanel";
import { PageHeader } from "@/components/page-header";
import { Panel, PanelBody } from "@/components/settings/panel";
import { SettingsRow } from "@/components/settings/row";
import SimpleFallbackPanel from "@/components/SimpleFallbackPanel";
import { Button } from "@/components/button";
import { DashboardSidecarPanels } from "@/features/gateway/Sidecars";
import { useGatewaySettings } from "@/features/gateway/use-settings";
import { Notice, Switch } from "@/ui";

export default function Dashboard({ apiBase }: { apiBase: string; refreshEpoch?: number }) {
  const d = useGatewaySettings(apiBase);
  return (
    <div className="gateway-settings grid gap-5">
      <PageHeader
        title={d.t("gateway.settingsTitle")}
        actions={
          <Button onClick={d.refresh}>{d.t(d.error ? "common.retry" : "startup.refresh")}</Button>
        }
      />
      {d.error && <Notice tone="err">{d.error}</Notice>}
      {d.applicationWarning && <Notice tone="warn">{d.applicationWarning}</Notice>}
      <DashboardSidecarPanels d={d} />
      <Panel id="proxy-compaction" title={d.t("proxyV2.secCompaction")}>
        <PanelBody>
          <SettingsRow
            label={d.t("dash.codexClientCompaction")}
            details={d.t("dash.codexClientCompactionHint")}
            control={
              <Switch
                on={d.settings?.codexClientCompaction === true}
                onClick={() => void d.toggleCompaction()}
                disabled={!d.settings || d.settingsSaving}
                label={d.t("dash.codexClientCompaction")}
              />
            }
          />
          <CompactionRoutingPanel apiBase={apiBase} models={d.models} providers={d.providers} />
        </PanelBody>
      </Panel>
      <SimpleFallbackPanel apiBase={apiBase} />
    </div>
  );
}
