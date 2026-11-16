import { useCallback, useEffect, useRef, useState } from "react";
import { readJsonOrThrow } from "../fetch-json";
import type { ChannelConfigurationEdit } from "@/contracts/channels";

export interface Config {
  port: number;
  defaultProvider: string;
  providers: Record<
    string,
    Record<string, unknown> & {
      adapter: string;
      baseUrl: string;
      hasApiKey?: boolean;
      hasHeaders?: boolean;
      xaiResponsesOptInState?: boolean | "mixed";
    }
  >;
}

type ConfigurationDraft = Omit<ChannelConfigurationEdit, "revision">;

function editableConfiguration({
  channels,
  default_channel_id,
  default_model_id,
}: ChannelConfigurationEdit): ConfigurationDraft {
  return { channels, default_channel_id, default_model_id };
}

export function useJsonConfigEditor(deps: {
  apiBase: string;
  config: Config | null;
  notify: (msg: string, ok?: boolean) => void;
  fetchConfig: () => Promise<void>;
  fetchProviderQuotas: (refresh?: boolean) => Promise<void>;
  onSaved: (addedProviders: string[]) => void;
  t: (key: string, values?: Record<string, string>) => string;
}) {
  const { apiBase, notify, fetchConfig, fetchProviderQuotas, onSaved, t } = deps;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [jsonEditorOpen, setJsonEditorOpen] = useState(false);
  const [jsonBaseline, setJsonBaseline] = useState("");
  const [jsonSaving, setJsonSaving] = useState(false);
  const [jsonLeaveOpen, setJsonLeaveOpen] = useState(false);
  const jsonEditorOpenRef = useRef(false);

  const revision = useRef<number | null>(null);
  const loadGeneration = useRef(0);
  useEffect(
    () => () => {
      loadGeneration.current++;
      revision.current = null;
    },
    [apiBase],
  );

  const saveConfig = useCallback(async (): Promise<boolean> => {
    setJsonSaving(true);
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft);
    } catch {
      notify(t("prov.invalidJson"), false);
      setJsonSaving(false);
      return false;
    }
    try {
      if (!parsed || typeof parsed !== "object" || revision.current === null) {
        notify(t("prov.invalidJson"), false);
        return false;
      }
      const baseline = JSON.parse(jsonBaseline) as ConfigurationDraft;
      const res = await fetch(apiBase + "/api/channels/configuration", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...parsed, revision: revision.current }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        notify(data.error || t("prov.saveFailed"), false);
        return false;
      }
      const saved = await readJsonOrThrow<ChannelConfigurationEdit>(res);
      if (!saved) throw new Error("Missing channel configuration.");
      notify(t("prov.saved"), true);
      setEditing(false);
      setJsonEditorOpen(false);
      jsonEditorOpenRef.current = false;
      setJsonLeaveOpen(false);
      setJsonBaseline(JSON.stringify(editableConfiguration(saved), null, 2));
      fetchConfig();
      fetchProviderQuotas(true);
      const previouslyConfigured = new Set(baseline.channels.map(({ id }) => id));
      const addedProviders = saved.channels
        .map(({ id }) => id)
        .filter((id) => !previouslyConfigured.has(id));
      onSaved(addedProviders);
      return true;
    } catch {
      notify(t("prov.saveFailed"), false);
      return false;
    } finally {
      setJsonSaving(false);
    }
  }, [apiBase, draft, fetchConfig, fetchProviderQuotas, jsonBaseline, notify, onSaved, t]);

  const openJsonEditor = useCallback(async () => {
    if (jsonEditorOpenRef.current) return;
    const generation = ++loadGeneration.current;
    try {
      const response = await fetch(apiBase + "/api/channels/configuration");
      const config = await readJsonOrThrow<ChannelConfigurationEdit>(response);
      if (generation !== loadGeneration.current) return;
      if (!config || !Array.isArray(config.channels) || !Number.isSafeInteger(config.revision))
        throw new Error("Missing channel configuration.");
      const baseline = JSON.stringify(editableConfiguration(config), null, 2);
      revision.current = config.revision;
      setJsonBaseline(baseline);
      setDraft(baseline);
      setJsonLeaveOpen(false);
      setJsonEditorOpen(true);
      jsonEditorOpenRef.current = true;
    } catch {
      if (generation === loadGeneration.current) notify(t("prov.loadConfigFail"), false);
    }
  }, [apiBase, notify, t]);

  const discardJsonEditor = useCallback(() => {
    loadGeneration.current++;
    setJsonLeaveOpen(false);
    setJsonEditorOpen(false);
    jsonEditorOpenRef.current = false;
    revision.current = null;
    setDraft(jsonBaseline);
  }, [jsonBaseline]);

  const requestCloseJsonEditor = useCallback(() => {
    if (jsonEditorOpen && draft !== jsonBaseline) {
      setJsonLeaveOpen(true);
      return;
    }
    discardJsonEditor();
  }, [discardJsonEditor, draft, jsonBaseline, jsonEditorOpen]);

  const restoreJsonEditor = useCallback(() => {
    setDraft(jsonBaseline);
  }, [jsonBaseline]);

  const jsonIsDirty = jsonEditorOpen && draft !== jsonBaseline;

  return {
    editing,
    setEditing,
    draft,
    setDraft,
    jsonEditorOpen,
    jsonBaseline,
    jsonSaving,
    jsonLeaveOpen,
    jsonEditorOpenRef,
    saveConfig,
    openJsonEditor,
    discardJsonEditor,
    requestCloseJsonEditor,
    restoreJsonEditor,
    jsonIsDirty,
    setJsonLeaveOpen,
  };
}
