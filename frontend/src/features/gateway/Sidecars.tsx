import { DataSurfaceSkeleton } from "@/components/data-surface";
import { HelpPopover } from "@/components/help-popover";
import { Panel, PanelBody } from "@/components/settings/panel";
import { IconChevron } from "@/icons";
import {
  clampVisionReasoningToLadder,
  parsePositiveInteger,
  parseVisionTimeoutMs,
  type SidecarPatch,
  VISION_MAX_DESCRIPTIONS_DEFAULT,
  VISION_TIMEOUT_MS_DEFAULT,
  visionEnabledPatch,
  visionMaxDescriptionsPatch,
  visionReasoningLadder,
  visionReasoningOptionsFor,
  visionReasoningPatch,
  visionSidecarBackendForModel,
  visionTimeoutPatch,
  webSearchEnabledPatch,
  webSearchSidecarSelectionForModel,
} from "@/pages/dashboard-shared";
import { VisionAdvancedPopover } from "@/pages/dashboard-vision-popover";
import { Select } from "@/ui";
import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { useGatewaySettings } from "./use-settings";
export function DashboardSidecarPanels({ d }: { d: ReturnType<typeof useGatewaySettings> }) {
  const { t, sidecar, sidecarSaving, sidecarModels, visionModels, models, saveSidecar } = d;
  const visionEnabled = sidecar?.vision?.enabled !== false;
  const visionModel = visionEnabled ? (sidecar?.vision?.model ?? "gpt-5.6-luna") : "";
  const webSearchEnabled = sidecar?.webSearch?.enabled !== false;
  // Same shape as the Vision card: Off is a row in the picker, and choosing a model is the way back.
  const webSearchModel = webSearchEnabled ? (sidecar?.webSearch?.model ?? "gpt-5.6-luna") : "";
  const persistedVisionReasoning = sidecar?.vision?.reasoning ?? "low";
  const visionLadder = visionReasoningLadder(models, visionModel);
  const visionReasoning = clampVisionReasoningToLadder(visionLadder, persistedVisionReasoning);
  const serverMaxDescriptions = String(
    sidecar?.vision?.maxDescriptionsPerTurn ?? VISION_MAX_DESCRIPTIONS_DEFAULT,
  );
  const serverTimeoutMs = String(sidecar?.vision?.timeoutMs ?? VISION_TIMEOUT_MS_DEFAULT);
  const [maxDraft, setMaxDraft] = useState<string | null>(null);
  const [timeoutDraft, setTimeoutDraft] = useState<string | null>(null);
  const [maxInvalid, setMaxInvalid] = useState(false);
  const [timeoutInvalid, setTimeoutInvalid] = useState(false);
  const [visionAdvancedOpen, setVisionAdvancedOpen] = useState(false);
  const visionAdvancedTriggerRef = useRef<HTMLButtonElement>(null);
  const maxValue = maxDraft ?? serverMaxDescriptions;
  const timeoutValue = timeoutDraft ?? serverTimeoutMs;

  const commitMaxDescriptions = (raw = maxValue) => {
    const parsed = parsePositiveInteger(raw);
    if (parsed === undefined) {
      setMaxDraft(raw);
      setMaxInvalid(true);
      return;
    }
    setMaxInvalid(false);
    setMaxDraft(null);
    if (parsed === (sidecar?.vision?.maxDescriptionsPerTurn ?? VISION_MAX_DESCRIPTIONS_DEFAULT))
      return;
    void saveSidecar(visionMaxDescriptionsPatch(parsed));
  };

  const commitTimeout = (raw = timeoutValue) => {
    const parsed = parseVisionTimeoutMs(raw);
    if (parsed === undefined) {
      setTimeoutDraft(raw);
      setTimeoutInvalid(true);
      return;
    }
    setTimeoutInvalid(false);
    setTimeoutDraft(null);
    if (parsed === (sidecar?.vision?.timeoutMs ?? VISION_TIMEOUT_MS_DEFAULT)) return;
    void saveSidecar(visionTimeoutPatch(parsed));
  };

  return (
    <Panel
      id="proxy-sidecars"
      title={t("proxyV2.secSidecars")}
      description={t("proxyV2.secSidecarsLede")}
    >
      <PanelBody>
        {!sidecar ? (
          <DataSurfaceSkeleton label={t("common.loading")} rows={2} />
        ) : (
          <>
            {/* Copy and controls share the gateway row layout so model pickers align. */}
            <div
              className="pv2-row dash-delegation-summary dash-sidecar-row-card"
              aria-busy={!sidecar || undefined}
            >
              <div className="dash-sidecar-copy">
                <div className="pv2-row-label">{t("dash.webSearchSidecar")}</div>
                <div className="muted setting-hint">{t("dash.webSearchSidecarHint")}</div>
              </div>
              <div className="dash-delegation-controls">
                <div className="dash-sidecar-select-row">
                  <Select
                    value={webSearchModel}
                    options={[{ value: "", label: t("dash.webSearchOff") }, ...sidecarModels]}
                    onChange={(model) => {
                      if (model === "") {
                        void saveSidecar(webSearchEnabledPatch(false));
                        return;
                      }
                      const patch: SidecarPatch = {
                        webSearch: webSearchSidecarSelectionForModel(models, sidecarModels, model),
                      };
                      // Choosing a model is the activation control: turning the sidecar back on from Off.
                      if (!webSearchEnabled)
                        patch.webSearch = { ...patch.webSearch, enabled: true };
                      void saveSidecar(patch);
                    }}
                    disabled={!sidecar || sidecarSaving}
                    label={t("dash.sidecarModel")}
                    align="right"
                  />
                </div>
                <div className="dash-sidecar-trailing-row">
                  <span className="muted setting-hint dash-sidecar-toggle-label">
                    {t("dash.webSearchStream")}
                  </span>
                  <HelpPopover label={t("dash.webSearchStream")}>
                    {t("dash.webSearchStreamHint")}
                  </HelpPopover>
                  <button
                    type="button"
                    className={`switch ${sidecar?.webSearch?.streamRoutedModelOutput ? "on" : ""}`}
                    onClick={() => {
                      void saveSidecar({
                        webSearch: {
                          streamRoutedModelOutput: !sidecar?.webSearch?.streamRoutedModelOutput,
                        },
                      });
                    }}
                    disabled={!webSearchEnabled || !sidecar || sidecarSaving}
                    aria-label={t("dash.webSearchStream")}
                    aria-pressed={sidecar?.webSearch?.streamRoutedModelOutput === true}
                  >
                    <span className="knob" />
                  </button>
                </div>
              </div>
            </div>

            <div
              className="pv2-row dash-delegation-summary dash-sidecar-row-card dash-vision-sidecar-card"
              aria-busy={!sidecar || undefined}
            >
              <div className="dash-sidecar-copy">
                <div className="pv2-row-label">{t("dash.visionSidecar")}</div>
                <div className="muted setting-hint">{t("dash.visionSidecarHint")}</div>
              </div>
              <div className="dash-delegation-controls">
                <div className="dash-sidecar-select-row">
                  <Select
                    value={visionModel}
                    options={[{ value: "", label: t("dash.visionOff") }, ...visionModels]}
                    onChange={(model) => {
                      if (model === "") {
                        void saveSidecar(visionEnabledPatch(false));
                        return;
                      }
                      const ladder = visionReasoningLadder(models, model);
                      const reasoning = clampVisionReasoningToLadder(ladder, visionReasoning);
                      const patch: SidecarPatch = {
                        vision: {
                          model,
                          backend: visionSidecarBackendForModel(models, visionModels, model),
                          reasoning,
                        },
                      };
                      // Choosing a model is the activation control: turning Vision back on from Off.
                      if (!visionEnabled) patch.vision = { ...patch.vision, enabled: true };
                      void saveSidecar(patch);
                    }}
                    disabled={!sidecar || sidecarSaving}
                    label={t("dash.sidecarModel")}
                  />
                  <Select
                    value={visionReasoning}
                    // Raw wire value (low…max), matching the delegation panel's bare `high`.
                    options={visionReasoningOptionsFor(visionLadder, visionReasoning).map(
                      (value) => ({
                        value,
                        label: value,
                      }),
                    )}
                    onChange={(reasoning) => {
                      void saveSidecar(visionReasoningPatch(reasoning as typeof visionReasoning));
                    }}
                    disabled={!visionEnabled || !sidecar || sidecarSaving}
                    align="right"
                    label={`${t("dash.visionSidecar")} — ${t("dash.injectionEffortLabel")}`}
                  />
                </div>
                <div className="dash-sidecar-trailing-row">
                  <button
                    type="button"
                    ref={visionAdvancedTriggerRef}
                    className="dash-vision-advanced-trigger"
                    onClick={() => setVisionAdvancedOpen((open) => !open)}
                    disabled={!visionEnabled || !sidecar || sidecarSaving}
                    aria-expanded={visionAdvancedOpen}
                    aria-haspopup="dialog"
                    aria-controls="dash-vision-advanced-popover"
                  >
                    <span>{t("dash.visionAdvanced")}</span>
                    <IconChevron
                      width={12}
                      height={12}
                      aria-hidden="true"
                      style={{
                        transform: visionAdvancedOpen ? "rotate(90deg)" : "none",
                        transition: "transform .12s",
                      }}
                    />
                  </button>
                </div>
              </div>
              {createPortal(
                <VisionAdvancedPopover
                  t={t}
                  open={visionAdvancedOpen}
                  triggerRef={visionAdvancedTriggerRef}
                  onClose={() => setVisionAdvancedOpen(false)}
                  maxValue={maxValue}
                  maxInvalid={maxInvalid}
                  timeoutValue={timeoutValue}
                  timeoutInvalid={timeoutInvalid}
                  disabled={!visionEnabled || !sidecar || sidecarSaving}
                  setMaxDraft={setMaxDraft}
                  setMaxInvalid={setMaxInvalid}
                  setTimeoutDraft={setTimeoutDraft}
                  setTimeoutInvalid={setTimeoutInvalid}
                  commitMaxDescriptions={commitMaxDescriptions}
                  commitTimeout={commitTimeout}
                />,
                document.body,
              )}
            </div>
          </>
        )}
      </PanelBody>
    </Panel>
  );
}
