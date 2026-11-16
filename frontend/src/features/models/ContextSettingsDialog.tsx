import { updateChannel } from "@/channel-management";
import { Button } from "@/components/button";
import type { ChannelSaved } from "@/contracts/channels";
import { readJsonOrThrow } from "@/fetch-json";
import { useI18n } from "@/i18n/shared";
import type { ProviderModelGroup } from "@/models-groups";
import { ownRecordValue } from "@/own-record-value";
import { parseContextWindowDraft, type ModelRow } from "@/pages/models-shared";
import { formatProviderDisplayName } from "@/provider-icons";
import { Notice, Select } from "@/ui";
import { useState } from "react";
export function ContextSettingsDialog({
  apiBase,
  group,
  onClose,
  onSaved,
}: {
  apiBase: string;
  group: ProviderModelGroup<ModelRow>;
  onClose: () => void;
  onSaved: (changed: boolean, application?: ChannelSaved["application"]) => Promise<void>;
}) {
  const { t } = useI18n();
  const contextModalProvider = group.provider;
  const contextModalModels = [
    ...new Set([
      ...group.rows.map((m) => m.id),
      ...group.configuredModels,
      ...Object.keys(group.modelContextWindows ?? {}),
    ]),
  ].sort();
  const [contextModelId, setContextModelId] = useState(contextModalModels[0] ?? "");
  const [contextDefaultDraft, setContextDefaultDraft] = useState(
    group.contextWindow ? String(group.contextWindow) : "",
  );
  const [contextModelDrafts, setContextModelDrafts] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      Object.entries(group.modelContextWindows ?? {}).map(([id, value]) => [id, String(value)]),
    ),
  );
  // What the modal showed when it opened. Every payload decision compares against THIS, not
  // against the live `groups`, because the 10s poll can refresh a value mid-modal: diffing
  // against current state would mark an untouched field dirty and revert someone else's change.
  const [contextSnapshot] = useState<{
    contextWindow: number | null;
    modelContextWindows: Record<string, number | null>;
  }>({
    contextWindow: group.contextWindow ?? null,
    modelContextWindows: { ...group.modelContextWindows },
  });
  // Which fields the USER typed into. Touch alone is not enough to send — a value typed and
  // then restored is not a change — but it is what makes an untouched field ineligible.
  const [contextTouchedModels, setContextTouchedModels] = useState<Set<string>>(new Set());
  const [contextDefaultTouched, setContextDefaultTouched] = useState(false);
  const [contextSaving, setContextSaving] = useState(false);
  const [contextError, setContextError] = useState("");
  const selectContextModel = (modelId: string) => {
    setContextModelId(modelId);
  };

  const saveContextSettings = async () => {
    if (!contextModalProvider) return;
    const providerWindow = parseContextWindowDraft(contextDefaultDraft);
    if (!group) {
      setContextError(t("models.contextSaveFailed"));
      return;
    }

    // A field is sent only when the user touched it AND its value actually differs from what
    // the modal opened with. Both halves matter, and each one alone is wrong.
    //
    // Sending only the selected model — what this did before — silently dropped any model
    // edited before switching the picker. No error, no warning, the value just did not save.
    //
    // Sending everything that differs from the LIVE state is wrong the other way: the 10s poll
    // can refresh a field mid-modal, and a stale draft would then look dirty and revert a
    // change the user never made. Comparing against the opening snapshot instead means a value
    // typed and then restored sends nothing at all.
    // Only validate the default when the user touched it. A malformed value inherited from a
    // hand-edited config would otherwise block a save that never intended to touch it.
    if (contextDefaultTouched && providerWindow === undefined) {
      setContextError(t("models.contextInvalid"));
      return;
    }
    const modelWindows: Record<string, number | null> = Object.create(null); // null prototype: a "__proto__" model ID must store an entry, not invoke the inherited setter
    for (const modelId of contextTouchedModels) {
      const draft = ownRecordValue(contextModelDrafts, modelId) ?? "";
      const parsed = parseContextWindowDraft(draft);
      if (parsed === undefined) {
        setContextError(t("models.contextInvalid"));
        return;
      }
      // Compare VALUES, not text. Retyping 64000 as "64,000" is not a change.
      if (parsed === (ownRecordValue(contextSnapshot.modelContextWindows, modelId) ?? null))
        continue;
      modelWindows[modelId] = parsed;
    }
    const defaultChanged =
      contextDefaultTouched && providerWindow !== contextSnapshot.contextWindow;

    // Nothing survived the comparison: every edit was reverted before Apply. Writing an
    // unchanged payload would still stamp over concurrent edits.
    if (!defaultChanged && Object.keys(modelWindows).length === 0) {
      onClose();
      // Not "updated" — nothing was. Saying otherwise would be a small lie the user could
      // act on, e.g. believing a value they typed and reverted had been written.
      onSaved(false);
      return;
    }

    let application: ChannelSaved["application"] | undefined;
    setContextSaving(true);
    setContextError("");
    try {
      const response = await updateChannel(apiBase, contextModalProvider, (detail) => {
        const channel = structuredClone(detail.channel);
        const metadata = channel.models?.metadata ?? {};
        if (defaultChanged)
          metadata.defaults = { ...metadata.defaults, contextWindow: providerWindow };
        metadata.overrides = { ...metadata.overrides };
        for (const [id, contextWindow] of Object.entries(modelWindows)) {
          metadata.overrides[id] = { ...metadata.overrides[id], contextWindow };
        }
        channel.models = { ...channel.models, metadata };
        return { channel, credential: { action: "keep" } };
      });
      const receipt = await readJsonOrThrow<ChannelSaved>(response, t("models.contextSaveFailed"));
      application = receipt?.application;
    } catch (error) {
      setContextError(error instanceof Error ? error.message : t("models.contextSaveFailed"));
      return;
    } finally {
      setContextSaving(false);
    }

    // Past the write boundary: the values ARE saved. A refresh that fails afterwards is a
    // display problem, and reporting it through `contextError` would set an error on a modal
    // that is already closed — invisible to the user, and it contradicts the success they just
    // saw. Let the ordinary load error surface handle it.
    onClose();
    await onSaved(true, application);
  };

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t("models.contextSettings")}
      onClick={() => {
        if (!contextSaving) onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !contextSaving) onClose();
      }}
    >
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h3>
            {t("models.contextSettingsTitle", {
              provider: formatProviderDisplayName(contextModalProvider, t),
            })}
          </h3>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onClose()}
            disabled={contextSaving}
            aria-label={t("common.close")}
          >
            &times;
          </Button>
        </div>

        {contextError && <Notice tone="err">{contextError}</Notice>}
        <p className="modal-desc leading-relaxed">{t("models.contextHint")}</p>

        <div className="models-context-fields">
          <label className="text-label models-field">
            {t("models.contextDefault")}
            <input
              className="input"
              inputMode="numeric"
              value={contextDefaultDraft}
              onChange={(event) => {
                setContextDefaultDraft(event.target.value);
                setContextDefaultTouched(true);
              }}
              disabled={contextSaving}
              placeholder={t("models.contextAutomatic")}
              autoFocus
            />
          </label>

          {contextModalModels.length > 0 && (
            <>
              <div className="text-label models-field">
                {t("models.contextModel")}
                <Select
                  value={contextModelId}
                  options={contextModalModels.map((model) => ({ value: model, label: model }))}
                  onChange={selectContextModel}
                  disabled={contextSaving}
                  label={t("models.contextModel")}
                />
              </div>
              <label className="text-label models-field">
                {t("models.contextModelOverride")}
                <input
                  className="input"
                  inputMode="numeric"
                  value={ownRecordValue(contextModelDrafts, contextModelId) ?? ""}
                  onChange={(event) => {
                    setContextModelDrafts((current) => ({
                      ...current,
                      [contextModelId]: event.target.value,
                    }));
                    setContextTouchedModels((current) => new Set(current).add(contextModelId));
                  }}
                  disabled={contextSaving}
                  placeholder={t("models.contextAutomatic")}
                />
              </label>
            </>
          )}
        </div>

        <div className="modal-actions">
          <Button
            type="button"
            variant="ghost"
            size="md"
            onClick={() => onClose()}
            disabled={contextSaving}
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            variant="primary"
            size="md"
            onClick={() => void saveContextSettings()}
            disabled={contextSaving}
          >
            {contextSaving ? t("models.customSaving") : t("models.customApply")}
          </Button>
        </div>
      </div>
    </div>
  );
}
