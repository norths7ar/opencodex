/**
 * Delegation settings for the Subagents tab.
 *
 * This panel used to sit on the Dashboard, which is otherwise a read-only status page — the
 * one place you could change something was also the first thing a new user saw. It reads
 * better next to the roster it affects: the roster picks who may be called, this picks who
 * gets called first.
 */
import { useLayoutEffect, useRef, useState } from "react";
import { Select } from "../../ui";
import { IconArrowDown, IconArrowUp, IconX } from "../../icons";
import { useT } from "../../i18n/shared";
import { formatNamespacedModelId } from "../../provider-icons";
import { clampNumberDraft } from "../../clamp-draft";
import { cn } from "@/lib/utils";
import { Button } from "../ui-v2/button";
import { Frame, FrameBody, HelpTip, SettingRow } from "../v2-subagents/frame";
import { iconButton, stepIndicator } from "../v2-subagents/frame-classes";
import { StepperInput } from "../v2-subagents/StepperInput";
import type { DelegationPatch, DelegationModelOption } from "../../pages/use-subagent-delegation";
import DelegationSuggest from "./DelegationSuggest";
import { EffortCaps } from "./EffortCaps";

export interface SubagentDelegationSectionProps {
  apiBase: string;
  model: string;
  effort: string;
  efforts: string[];
  available: DelegationModelOption[];
  syncCodexDefaults: boolean;
  saving: boolean;
  onSave: (patch: DelegationPatch) => void;
  fallback: string[];
  fallbackLoadState?: "loading" | "ready" | "error";
  fallbackPollMs: number;
  fallbackBusy: boolean;
  availableModels: string[];
  onFallbackChange: (models: string[]) => void;
  onFallbackPollMsChange: (pollMs: number) => void;
  onFallbackSave: () => void;
  id?: string;
}

export default function SubagentDelegationSection({
  apiBase,
  model,
  effort,
  efforts,
  available,
  syncCodexDefaults,
  saving,
  onSave,
  fallback,
  fallbackLoadState = "ready",
  fallbackPollMs,
  fallbackBusy,
  availableModels,
  onFallbackChange,
  onFallbackPollMsChange,
  onFallbackSave,
  id,
}: SubagentDelegationSectionProps) {
  const t = useT();
  const availableModelSet = new Set(availableModels);
  const fallbackSet = new Set(fallback);
  const [pollDraft, setPollDraft] = useState(() => ({
    pollMs: fallbackPollMs,
    text: String(fallbackPollMs),
  }));
  // Keep blank/invalid input text while reconciling accepted settings from a load or save.
  if (!Object.is(pollDraft.pollMs, fallbackPollMs)) {
    setPollDraft({
      pollMs: fallbackPollMs,
      text: Number.isFinite(fallbackPollMs) ? String(fallbackPollMs) : "",
    });
  }
  const fallbackControlsRef = useRef<HTMLDivElement>(null);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [identity, setIdentity] = useState(() => ({
    models: fallback,
    rows: fallback.map((rowModel, id) => ({ model: rowModel, id })),
    nextId: fallback.length,
  }));
  let rows = identity.rows;
  // Keys are render state. Guarded prop reconciliation retains each occurrence;
  // event handlers move the same identities with their corresponding models.
  if (identity.models !== fallback) {
    const remaining = [...identity.rows];
    let nextId = identity.nextId;
    rows = fallback.map((modelName) => {
      const old = remaining.findIndex((row) => row.model === modelName);
      return old >= 0 ? remaining.splice(old, 1)[0] : { model: modelName, id: nextId++ };
    });
    setIdentity({ models: fallback, rows, nextId });
  }
  const pendingFocus = useRef<{ row: number; action: string } | null>(null);
  useLayoutEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    pendingFocus.current = null;
    const row = fallbackControlsRef.current?.querySelectorAll(".swi-fallback-row")[target.row];
    const enabledActions = row?.querySelectorAll<HTMLButtonElement>(
      "button[data-action]:not(:disabled)",
    );
    const action =
      Array.from(enabledActions ?? []).find((button) => button.dataset.action === target.action) ??
      row?.querySelector<HTMLButtonElement>("button:not(:disabled)") ??
      fallbackControlsRef.current?.querySelector<HTMLButtonElement>('button[role="combobox"]');
    action?.focus();
  }, [fallback]);
  const validPollMs =
    Number.isInteger(fallbackPollMs) && fallbackPollMs >= 5000 && fallbackPollMs <= 600000;
  const moveFallback = (index: number, direction: -1 | 1) => {
    const next = [...fallback];
    const target = index + direction;
    if (fallbackBusy || target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    const nextRows = [...rows];
    [nextRows[index], nextRows[target]] = [nextRows[target], nextRows[index]];
    setIdentity({ ...identity, models: next, rows: nextRows });
    pendingFocus.current = { row: target, action: direction === -1 ? "up" : "down" };
    onFallbackChange(next);
  };

  const editPoll = (text: string) => {
    const parsed = Number(text);
    const pollMs = text.trim() !== "" && Number.isFinite(parsed) ? parsed : Number.NaN;
    setPollDraft({ pollMs, text });
    onFallbackPollMsChange(pollMs);
  };
  const pollLabel = t("sub.fallbackPoll");

  // A settings form after React Bits settings-form-3: one row per decision, the plain label
  // and a one-line hint on the left, the control on the right; the long hints moved behind "?".
  return (
    <Frame id={id} className="swi-delegation" title={t("subagentsV2.delegationTitle")}>
      <FrameBody>
        <SettingRow
          className="swi-delegation-row swi-delegation-model-row"
          label={t("sub.delegation.model")}
          hint={t("subagentsV2.delegationShort")}
          help={
            <HelpTip label={t("sub.delegation.model")}>{t("sub.delegation.modelHint")}</HelpTip>
          }
          control={
            <>
              <Select
                value={model}
                options={[
                  { value: "", label: t("dash.injectionNone") },
                  ...available.map((m) => ({
                    value: m.namespaced,
                    label: formatNamespacedModelId(`${m.provider}/${m.model}`, t),
                  })),
                ]}
                onChange={(v) => onSave({ model: v || null, effort: effort || null })}
                disabled={saving}
                label={t("dash.injectionLabel")}
                align="right"
              />
              {model && efforts.length > 0 && (
                <Select
                  value={effort}
                  options={[
                    { value: "", label: t("dash.injectionEffortNone") },
                    ...efforts.map((e) => ({ value: e, label: e })),
                  ]}
                  onChange={(v) => onSave({ model: model || null, effort: v || null })}
                  disabled={saving}
                  label={t("dash.injectionEffortLabel")}
                  align="right"
                />
              )}
              <Button
                size="sm"
                variant={suggestOpen ? "outline" : "ghost"}
                aria-expanded={suggestOpen}
                onClick={() => setSuggestOpen((open) => !open)}
              >
                {t("sub.suggest.button")}
              </Button>
            </>
          }
        >
          {suggestOpen && (
            <DelegationSuggest
              apiBase={apiBase}
              model={model}
              effort={effort}
              available={available}
              saving={saving}
              onAccept={onSave}
            />
          )}
        </SettingRow>

        <SettingRow
          className="swi-delegation-row swi-fallback-editor"
          label={t("sub.fallbackLabel")}
          hint={t("sub.fallbackHint")}
        >
          {fallbackLoadState === "loading" && <p role="status">{t("common.loading")}</p>}
          {fallbackLoadState === "ready" && (
            <div
              className="swi-fallback-controls flex min-w-0 flex-col gap-2"
              ref={fallbackControlsRef}
            >
              {fallback.length > 0 && (
                <ol className="m-0 flex list-none flex-col divide-y divide-line overflow-hidden rounded-[8px] p-0 ring-1 ring-line">
                  {fallback.map((modelName, index) => (
                    <li
                      key={rows[index].id}
                      className="swi-fallback-row flex min-h-10 min-w-0 items-center gap-3 py-1 pr-1.5 pl-2.5"
                    >
                      <span
                        className={cn(
                          stepIndicator,
                          "swi-fallback-pos bg-surface text-fg ring-line-strong",
                        )}
                      >
                        {index + 1}
                      </span>
                      <span className="swi-fallback-model flex min-w-0 flex-1 flex-col">
                        <span className="swi-fallback-name truncate font-mono text-[13px] text-fg">
                          {modelName}
                        </span>
                        {!availableModelSet.has(modelName) && (
                          <span className="text-[12px] text-warning">
                            {t("sub.fallbackUnavailable")}
                          </span>
                        )}
                      </span>
                      <span className="swi-fallback-actions flex shrink-0 items-center gap-0.5">
                        <button
                          type="button"
                          className={iconButton}
                          data-action="up"
                          onClick={() => moveFallback(index, -1)}
                          disabled={fallbackBusy || index === 0}
                          aria-label={t("sub.moveUp", { m: modelName })}
                        >
                          <IconArrowUp />
                        </button>
                        <button
                          type="button"
                          className={iconButton}
                          data-action="down"
                          onClick={() => moveFallback(index, 1)}
                          disabled={fallbackBusy || index === fallback.length - 1}
                          aria-label={t("sub.moveDown", { m: modelName })}
                        >
                          <IconArrowDown />
                        </button>
                        <button
                          type="button"
                          className={cn(iconButton, "hover:bg-danger-soft hover:text-danger")}
                          data-action="remove"
                          onClick={() => {
                            const next = fallback.filter((_, i) => i !== index);
                            setIdentity({
                              ...identity,
                              models: next,
                              rows: rows.filter((_, i) => i !== index),
                            });
                            pendingFocus.current = {
                              row: Math.max(0, Math.min(index, fallback.length - 2)),
                              action: "remove",
                            };
                            onFallbackChange(next);
                          }}
                          disabled={fallbackBusy}
                          aria-label={t("sub.removeAria", { m: modelName })}
                        >
                          <IconX />
                        </button>
                      </span>
                    </li>
                  ))}
                </ol>
              )}
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <Select
                  value=""
                  label={t("sub.fallbackAdd")}
                  options={[
                    { value: "", label: t("sub.fallbackAdd") },
                    ...availableModels
                      .filter((modelName) => !fallbackSet.has(modelName))
                      .map((modelName) => ({ value: modelName, label: modelName })),
                  ]}
                  onChange={(value) => {
                    if (value && !fallbackSet.has(value)) onFallbackChange([...fallback, value]);
                  }}
                  disabled={fallbackBusy}
                />
                <div className="ml-auto flex items-center gap-2 text-[13px] text-fg-muted">
                  {/* A wrapping <label> would name the first labelable descendant, the decrement
                    button; tie the label to the input itself instead. */}
                  <label htmlFor="subagent-fallback-poll-ms">{pollLabel}</label>
                  <StepperInput
                    id="subagent-fallback-poll-ms"
                    min={5000}
                    max={600000}
                    step={1000}
                    value={pollDraft.text}
                    onChange={(e) => editPoll(e.currentTarget.value)}
                    disabled={fallbackBusy}
                    aria-invalid={!validPollMs}
                    stepDisabled={fallbackBusy}
                    unit="ms"
                    inputClassName="w-24"
                    decrementLabel={t("workflowBudget.decrease", { label: pollLabel })}
                    incrementLabel={t("workflowBudget.increase", { label: pollLabel })}
                    onDecrement={() =>
                      editPoll(clampNumberDraft(pollDraft.text, -1000, 5000, 600000))
                    }
                    onIncrement={() =>
                      editPoll(clampNumberDraft(pollDraft.text, 1000, 5000, 600000))
                    }
                  />
                </div>
                <Button
                  size="sm"
                  variant="primary"
                  onClick={onFallbackSave}
                  disabled={fallbackBusy || !validPollMs}
                >
                  {t("common.save")}
                </Button>
              </div>
              {!validPollMs && (
                <div className="text-[12px] text-danger" role="alert">
                  {t("sub.fallbackPollInvalid")}
                </div>
              )}
            </div>
          )}
        </SettingRow>

        <SettingRow
          className="swi-delegation-row"
          label={t("dash.syncCodexSubagentDefaults")}
          hint={t("subagentsV2.syncShort")}
          help={
            <HelpTip label={t("dash.syncCodexSubagentDefaults")}>
              {t("dash.syncCodexSubagentDefaultsHint")}
            </HelpTip>
          }
          control={
            <button
              type="button"
              className={`switch ${syncCodexDefaults ? "on" : ""}`}
              onClick={() => onSave({ syncCodexSubagentDefaults: !syncCodexDefaults })}
              disabled={saving || !model}
              aria-label={t("dash.syncCodexSubagentDefaults")}
              aria-pressed={syncCodexDefaults}
            >
              <span className="knob" />
            </button>
          }
        />

        <EffortCaps apiBase={apiBase} />
      </FrameBody>
    </Frame>
  );
}
