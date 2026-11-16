import {
  MetadataSource,
  ModelCostSummary,
  ModelReasoningSummary,
} from "@/components/model-metadata";
import { Button } from "@/components/button";
import { useI18n } from "@/i18n/shared";
import { modelLabel } from "@/model-display";
import { fmtK, type ModelRow } from "@/pages/models-shared";
import { formatProviderDisplayName } from "@/provider-icons";
import { Switch } from "@/ui";
import { useEffect, useRef, useState } from "react";
export function CatalogModelRow({
  model: m,
  selected,
  alias,
  recent,
  contextCapValue,
  disabled,
  onToggle,
  onEdit,
  onDelete,
}: {
  model: ModelRow;
  selected: boolean;
  alias?: { alias: string; source: "user" | "builtin"; stale?: boolean };
  recent: boolean;
  contextCapValue: number;
  disabled: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onDelete: (id: string, name: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const off = !selected;
  const [hoveredModel, setHoveredModel] = useState<{ namespaced: string; rect: DOMRect } | null>(
    null,
  );
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    },
    [],
  );
  const onRowEnter = (namespaced: string, el: HTMLElement) => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = setTimeout(() => {
      setHoveredModel({ namespaced, rect: el.getBoundingClientRect() });
    }, 300);
  };

  const onRowFocus = (namespaced: string, el: HTMLElement) => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    setHoveredModel({ namespaced, rect: el.getBoundingClientRect() });
  };

  const onRowLeave = () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = setTimeout(() => setHoveredModel(null), 120);
  };

  const keepRowTipOpen = () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
  };

  return (
    <div
      className="model-row-wrap"
      onMouseEnter={(e) => onRowEnter(m.namespaced, e.currentTarget)}
      onMouseLeave={onRowLeave}
      onFocus={(e) => onRowFocus(m.namespaced, e.currentTarget)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setHoveredModel(null);
      }}
    >
      <div className="row models-model-row">
        <Switch
          on={!off}
          onClick={onToggle}
          disabled={disabled || m.initialSelectionPending}
          label={m.native ? m.id : m.namespaced}
        />
        <span className="models-model-main">
          {alias && <strong className="mono text-control">{alias.alias}</strong>}
          <span className="models-model-identity">
            <code
              className="mono text-control"
              style={{
                color: off ? "var(--ocx-fg-subtle)" : "var(--ocx-fg)",
                textDecoration: off ? "line-through" : "none",
              }}
            >
              {m.native ? modelLabel(m.id) : m.namespaced}
            </code>
            {!m.native && m.displayName?.trim() && m.displayName.trim() !== m.namespaced && (
              <span className="models-model-friendly text-caption">{m.displayName.trim()}</span>
            )}
          </span>
          {m.initialSelectionPending && (
            <span className="models-chip muted" role="status">
              {t("models.initialSelectionPending")}
            </span>
          )}
          {/* #1711: listed and selectable, but every usable target is out of credit.
                        Not a visibility change and not the operator's disable flag — the row is
                        still offered, which is what the issue asks for. */}
          {m.quotaInactiveReason === "no_credit" && (
            <span
              className="models-chip muted"
              role="status"
              title={t("models.inactiveNoCreditHint")}
            >
              {t("models.inactiveNoCredit")}
            </span>
          )}
          {alias?.source === "builtin" && (
            <span className="models-chip muted text-caption">{t("models.aliasAuto")}</span>
          )}
          {m.custom && (
            <span className="models-chip muted mono text-caption">{t("models.customBadge")}</span>
          )}
          {!m.native && m.provider !== "combo" && m.manualPricing === true && (
            <span className="models-chip muted text-caption">{t("pricing.override.badge")}</span>
          )}
          {!m.custom && recent && <span className="badge badge-amber">{t("models.newBadge")}</span>}
          {m.contextCapped && (
            <span className="models-chip muted mono text-caption">
              {t("models.contextCappedValue", {
                value: fmtK(m.contextCap ?? contextCapValue),
              })}
            </span>
          )}
        </span>
        {/* Per-model edits, right-aligned like a table's action column. */}
        <span className="models-model-actions" role="group" aria-label={t("modelsV2.modelActions")}>
          {!m.native && m.provider !== "combo" && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="text-caption models-display-name-trigger"
              aria-haspopup="dialog"
              aria-label={t("models.settingsTitle", { model: m.namespaced })}
              onClick={() => {
                setHoveredModel(null);
                onEdit();
              }}
            >
              {t("models.customEdit")}
            </Button>
          )}
        </span>
      </div>
      {hoveredModel?.namespaced === m.namespaced &&
        (() => {
          const r = hoveredModel.rect;
          const tipTop = r.bottom + 4;
          const below = window.innerHeight - tipTop - 8;
          const above = r.top - 12;
          const flipUp = below < 360 && above > below;
          return (
            <div
              className={`model-tip${m.custom ? " has-actions" : ""}${flipUp ? " flip-up" : ""}`}
              role="tooltip"
              style={{
                position: "fixed",
                left: Math.max(8, Math.min(r.left + 24, window.innerWidth - 488)),
                maxHeight: Math.max(64, Math.min(360, flipUp ? above : below)),
                ...(flipUp ? { bottom: window.innerHeight - r.top + 4 } : { top: tipTop }),
              }}
              onMouseEnter={keepRowTipOpen}
              onMouseLeave={onRowLeave}
            >
              <div className="model-tip-id">{m.native ? m.id : m.namespaced}</div>
              {m.displayName && (
                <div className="model-tip-display">
                  {m.displayName}{" "}
                  {m.metadataSources?.displayName && (
                    <MetadataSource value={m.metadataSources.displayName} />
                  )}
                </div>
              )}
              {m.custom && (
                <span className="models-chip models-chip--tip muted mono text-caption">
                  {t("models.customBadge")}
                </span>
              )}
              <div className="model-tip-grid">
                <span className="model-tip-key">{t("models.tipProvider")}</span>
                <span className="model-tip-val">{formatProviderDisplayName(m.provider, t)}</span>
                <span className="model-tip-key">{t("models.tipContext")}</span>
                <span className="model-tip-val">
                  {m.contextWindow?.toLocaleString() ?? t("modelUnknown")}
                  {m.contextCapped && (
                    <>
                      {" "}
                      ·{" "}
                      {t("models.contextCappedValue", {
                        value: fmtK(m.contextCap ?? 0),
                      })}
                    </>
                  )}
                  {m.metadataSources?.contextWindow && (
                    <span className="model-summary-line">
                      <MetadataSource value={m.metadataSources.contextWindow} />
                    </span>
                  )}
                </span>
                <span className="model-tip-key">{t("models.tipModalities")}</span>
                <span className="model-tip-val">
                  {m.inputModalities?.join(", ") || t("modelUnknown")}
                  {m.metadataSources?.inputModalities && (
                    <span className="model-summary-line">
                      <MetadataSource value={m.metadataSources.inputModalities} />
                    </span>
                  )}
                </span>
                <span className="model-tip-key">{t("modelOutputModalities")}</span>
                <span className="model-tip-val">
                  {m.outputModalities?.join(", ") || t("modelUnknown")}
                  {m.metadataSources?.outputModalities && (
                    <span className="model-summary-line">
                      <MetadataSource value={m.metadataSources.outputModalities} />
                    </span>
                  )}
                </span>
                {m.maxInputTokens !== undefined && (
                  <>
                    <span className="model-tip-key">{t("modelInputLimit")}</span>
                    <span className="model-tip-val">
                      {m.maxInputTokens.toLocaleString()}
                      {m.metadataSources?.maxInputTokens && (
                        <span className="model-summary-line">
                          <MetadataSource value={m.metadataSources.maxInputTokens} />
                        </span>
                      )}
                    </span>
                  </>
                )}
                <span className="model-tip-key">{t("modelOutputLimit")}</span>
                <span className="model-tip-val">
                  {m.maxOutputTokens?.toLocaleString() ?? t("modelUnknown")}
                  {m.metadataSources?.maxOutputTokens && (
                    <span className="model-summary-line">
                      <MetadataSource value={m.metadataSources.maxOutputTokens} />
                    </span>
                  )}
                </span>
                <span className="model-tip-key">{t("models.customFieldReasoning")}</span>
                <span className="model-tip-val">
                  <ModelReasoningSummary row={m} />
                  {(m.metadataSources?.reasoningEfforts ||
                    m.metadataSources?.supportsReasoning) && (
                    <span className="model-summary-line">
                      <MetadataSource
                        value={
                          m.metadataSources.reasoningEfforts ?? m.metadataSources.supportsReasoning
                        }
                      />
                    </span>
                  )}
                </span>
                <span className="model-tip-key">{t("modelPricing")}</span>
                <span className="model-tip-val">
                  <ModelCostSummary
                    cost={m.effectiveCost}
                    schedule={m.pricingSchedule}
                    subscription={m.subscription}
                  />
                  {m.metadataSources?.effectiveCost && (
                    <span className="model-summary-line">
                      <MetadataSource value={m.metadataSources.effectiveCost} />
                    </span>
                  )}
                </span>
                <span className="model-tip-key">{t("models.tipStatus")}</span>
                <span className="model-tip-val">
                  {off ? t("models.tipDisabled") : t("models.tipActive")}
                </span>
              </div>
              {m.custom && m.customId && (
                <div className="model-tip-actions">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="text-caption"
                    style={{ color: "var(--ocx-danger)" }}
                    onClick={() => {
                      // Hover is cleared AFTER the dialog closes, not before it
                      // opens: dropping it first unmounts this button, and the
                      // dialog then has nothing to return focus to.
                      void onDelete(m.customId!, m.displayName ?? m.id).finally(() =>
                        setHoveredModel(null),
                      );
                    }}
                  >
                    {t("models.customDelete")}
                  </Button>
                </div>
              )}
            </div>
          );
        })()}
    </div>
  );
}
