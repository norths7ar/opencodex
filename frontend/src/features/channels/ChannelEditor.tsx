import { Button } from "@/components/button";
import type {
  ApplicationReceipt,
  Channel,
  ChannelDetail,
  ChannelSaved,
  SaveChannel,
} from "@/contracts/channels";
import { readJsonOrThrow } from "@/fetch-json";
import { Notice, Select, Switch } from "@/ui";
import { useState } from "react";
import { useChannelCopy } from "./copy";

export type ServicePreset = {
  id: string;
  serviceId?: string;
  label: string;
  adapter: Channel["protocol"];
  baseUrl: string;
  auth: "key" | "oauth" | "local";
  defaultModel?: string;
};

export function ChannelEditor({
  apiBase,
  detail,
  presets,
  onSaved,
  onClose,
  onRemove,
  removing = false,
}: {
  apiBase: string;
  detail?: ChannelDetail;
  presets: ServicePreset[];
  onSaved: (id: string, application?: ApplicationReceipt) => void;
  onClose: () => void;
  onRemove?: () => void;
  removing?: boolean;
}) {
  const c = useChannelCopy();
  const [channel, setChannel] = useState<Channel>(
    () =>
      detail?.channel ?? {
        id: "",
        name: "",
        service_id: "openai-api",
        protocol: "openai-chat",
        auth_mode: "key",
        endpoints: { base_url: "https://api.openai.com/v1" },
        models: { live: true },
      },
  );
  const [credentialAction, setCredentialAction] = useState<
    "keep" | "set_key" | "set_environment" | "clear"
  >("keep");
  const [secret, setSecret] = useState("");
  const [headersKeep, setHeadersKeep] = useState(true);
  const [headers, setHeaders] = useState("{}");
  const [policyChanged, setPolicyChanged] = useState(false);
  const [policy, setPolicy] = useState(JSON.stringify(channel.request_policy ?? {}, null, 2));
  const [busy, setBusy] = useState(false);
  const submitting = busy || removing;
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof Channel>(key: K, value: Channel[K]) =>
    setChannel((old) => ({ ...old, [key]: value }));
  const resetCredential = () => {
    setCredentialAction("keep");
    setSecret("");
  };
  const preset = (id: string) => {
    const value = presets.find((item) => item.id === id);
    if (!value) return;
    resetCredential();
    setChannel((old) => ({
      ...old,
      service_id: value.serviceId ?? value.id,
      name: value.label,
      protocol: value.adapter,
      auth_mode: value.auth,
      endpoints: { base_url: value.baseUrl },
      oauth_binding:
        value.auth === "oauth" ? { service_id: "chatgpt", account_ref: "chatgpt" } : null,
      models: { ...old.models, default: value.defaultModel ?? null },
    }));
  };
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const edit: SaveChannel = {
        channel: {
          ...channel,
          id: channel.id.trim(),
          name: channel.name.trim(),
          ...(policyChanged ? { request_policy: JSON.parse(policy) } : {}),
        },
        credential:
          channel.auth_mode !== "key"
            ? { action: "keep" }
            : credentialAction === "set_key"
              ? { action: "set_key", value: secret }
              : credentialAction === "set_environment"
                ? { action: "set_environment", variable: secret }
                : { action: credentialAction },
        ...(!headersKeep ? { headers: JSON.parse(headers) } : {}),
      };
      const path = detail
        ? `/api/channels/${encodeURIComponent(channel.id)}?revision=${detail.revision}`
        : "/api/channels";
      const receipt = await fetch(apiBase + path, {
        method: detail ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(edit),
      }).then(readJsonOrThrow<ChannelSaved>);
      onSaved(receipt?.channel.id ?? edit.channel.id, receipt?.application);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : c("error"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="channel-editor" aria-busy={submitting}>
      <div className="channel-form-grid">
        {!detail && (
          <label>
            {c("service")}
            <Select
              value={
                presets.find((item) => (item.serviceId ?? item.id) === channel.service_id)?.id ?? ""
              }
              options={presets.map((item) => ({ value: item.id, label: item.label }))}
              onChange={preset}
              label={c("service")}
            />
          </label>
        )}
        <label>
          {c("id")}
          <input
            className="input"
            value={channel.id}
            disabled={!!detail}
            onChange={(e) => set("id", e.target.value)}
          />
        </label>
        <label>
          {c("name")}
          <input
            className="input"
            value={channel.name}
            onChange={(e) => set("name", e.target.value)}
          />
        </label>
        <label>
          {c("protocol")}
          <Select
            value={channel.protocol}
            options={[
              { value: "openai-chat", label: "Chat Completions" },
              { value: "openai-responses", label: "Responses" },
              { value: "anthropic", label: "Messages" },
            ]}
            onChange={(value) => set("protocol", value as Channel["protocol"])}
            label={c("protocol")}
          />
        </label>
        {channel.auth_mode !== "oauth" && (
          <label>
            {c("authMode")}
            <Select
              value={channel.auth_mode}
              options={[
                { value: "key", label: c("key") },
                { value: "local", label: c("local") },
              ]}
              onChange={(value) => {
                resetCredential();
                set("auth_mode", value as "key" | "local");
              }}
              label={c("authMode")}
            />
          </label>
        )}
        <label className="channel-form-wide">
          {c("baseUrl")}
          <input
            className="input"
            type="url"
            value={channel.endpoints.base_url}
            onChange={(e) => set("endpoints", { ...channel.endpoints, base_url: e.target.value })}
          />
        </label>
        <label>
          {c("defaultModel")}
          <input
            className="input"
            value={channel.models?.default ?? ""}
            onChange={(e) => set("models", { ...channel.models, default: e.target.value || null })}
            placeholder={c("inherit")}
          />
        </label>
        <label>
          {c("note")}
          <input
            className="input"
            value={channel.note ?? ""}
            onChange={(e) => set("note", e.target.value || null)}
          />
        </label>
      </div>
      <div className="channel-form-grid channel-toggle-grid">
        <div className="channel-toggle">
          <span>{c("enabled")}</span>
          <Switch
            on={!channel.disabled}
            onClick={() => set("disabled", !channel.disabled)}
            label={c("enabled")}
          />
        </div>
        <div className="channel-toggle">
          <span>{c("discovery")}</span>
          <Switch
            on={channel.models?.live !== false}
            onClick={() =>
              set("models", { ...channel.models, live: channel.models?.live === false })
            }
            label={c("discovery")}
          />
        </div>
      </div>
      {channel.auth_mode === "key" && (
        <div className="channel-form-grid">
          <label>
            {c("transport")}
            <Select
              value={channel.api_key_transport ?? ""}
              options={[
                { value: "", label: c("inherit") },
                { value: "bearer", label: "Authorization: Bearer" },
                { value: "x-api-key", label: "x-api-key" },
              ]}
              onChange={(value) =>
                set("api_key_transport", value ? (value as "bearer" | "x-api-key") : null)
              }
              label={c("transport")}
            />
          </label>
          <label>
            {c("credential")}
            <Select
              value={credentialAction}
              options={[
                { value: "keep", label: c("keep") },
                { value: "set_key", label: c("key") },
                { value: "set_environment", label: c("environment") },
                { value: "clear", label: c("clear") },
              ]}
              onChange={(value) => {
                setCredentialAction(value as typeof credentialAction);
                setSecret("");
              }}
              label={c("credential")}
            />
          </label>
          {(credentialAction === "set_key" || credentialAction === "set_environment") && (
            <label className="channel-form-wide">
              {c(credentialAction === "set_key" ? "key" : "environment")}
              <input
                className="input"
                type={credentialAction === "set_key" ? "password" : "text"}
                autoComplete="off"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
              />
            </label>
          )}
        </div>
      )}
      <details>
        <summary>{c("advanced")}</summary>
        <div className="channel-form-grid">
          {(["discovery", "chat", "responses", "messages"] as const).map((key) => (
            <label key={key}>
              {key}
              <input
                className="input"
                value={channel.endpoints[key] ?? ""}
                onChange={(e) =>
                  set("endpoints", { ...channel.endpoints, [key]: e.target.value || null })
                }
              />
            </label>
          ))}
          <label className="channel-form-wide">
            {c("modelIds")}
            <textarea
              className="input mono"
              rows={3}
              value={(channel.models?.static ?? []).join("\n")}
              onChange={(e) =>
                set("models", {
                  ...channel.models,
                  static: e.target.value.split("\n").filter(Boolean),
                })
              }
            />
          </label>
          <div className="channel-toggle">
            <span>{c("privacy")}</span>
            <Switch
              on={channel.allow_private_network === true}
              onClick={() => set("allow_private_network", !channel.allow_private_network)}
              label={c("privacy")}
            />
          </div>
          <div className="channel-toggle">
            <span>{c("pacing")}</span>
            <Switch
              on={channel.request_pacing?.enabled === true}
              onClick={() =>
                set("request_pacing", {
                  ...channel.request_pacing,
                  enabled: !channel.request_pacing?.enabled,
                })
              }
              label={c("pacing")}
            />
          </div>
          {(
            [
              ["requestsPerMinute", "rpm"],
              ["minIntervalMs", "interval"],
              ["maxConcurrentRequests", "concurrency"],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              {c(label)}
              <input
                className="input"
                type="number"
                min="0"
                value={channel.request_pacing?.[key] ?? ""}
                onChange={(e) =>
                  set("request_pacing", {
                    enabled: channel.request_pacing?.enabled ?? false,
                    ...channel.request_pacing,
                    [key]: e.target.value ? Number(e.target.value) : null,
                  })
                }
              />
            </label>
          ))}
          <div className="channel-toggle channel-form-wide">
            <span>{c("headersKeep")}</span>
            <Switch
              on={headersKeep}
              onClick={() => setHeadersKeep(!headersKeep)}
              label={c("headersKeep")}
            />
          </div>
          {!headersKeep && (
            <label className="channel-form-wide">
              {c("headers")}
              <textarea
                className="input mono"
                rows={4}
                value={headers}
                onChange={(e) => setHeaders(e.target.value)}
                spellCheck={false}
              />
            </label>
          )}
          <label className="channel-form-wide">
            {c("policy")}
            <textarea
              className="input mono"
              rows={6}
              value={policy}
              onChange={(e) => {
                setPolicy(e.target.value);
                setPolicyChanged(true);
              }}
              spellCheck={false}
            />
            <small>{c("policyHint")}</small>
          </label>
        </div>
      </details>
      {error && <Notice tone="err">{error}</Notice>}
      <div className="channel-editor-footer">
        {onRemove && (
          <Button
            variant="danger"
            size="sm"
            onClick={onRemove}
            disabled={submitting}
            aria-busy={removing}
          >
            {c("remove")}
          </Button>
        )}
        <div className="channel-actions channel-editor-submit">
          <Button size="sm" onClick={onClose} disabled={submitting}>
            {c("cancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void save()}
            disabled={submitting || !channel.id.trim() || !channel.endpoints.base_url.trim()}
          >
            {c("save")}
          </Button>
        </div>
      </div>
    </section>
  );
}
