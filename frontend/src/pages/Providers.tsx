import { confirmAction } from "@/action-dialogs";
import { DataSurfaceSkeleton } from "@/components/data-surface";
import { PageHeader } from "@/components/page-header";
import { PlannedFeature } from "@/components/planned-feature";
import ProviderCompletionTest from "@/components/provider-workspace/ProviderCompletionTest";
import { Button } from "@/components/button";
import { ProviderMark } from "@/components/v2-models/provider-mark";
import { useDataSurface } from "@/data-surface";
import { AccountPanel } from "@/features/channels/AccountPanel";
import { readChannels, refreshChannelModels, setDefaultChannel } from "@/features/channels/api";
import { ChannelEditor, type ServicePreset } from "@/features/channels/ChannelEditor";
import "@/features/channels/channels.css";
import { useChannelCopy } from "@/features/channels/copy";
import { CredentialLabel } from "@/features/channels/CredentialLabel";
import { readJsonOrThrow } from "@/fetch-json";
import type { ApplicationReceipt, ChannelDetail } from "@/contracts/channels";
import { useT } from "@/i18n/shared";
import { clientApplicationWarning } from "@/contracts/application";
import { navigateHash } from "@/hash-routing";
import {
  IconBoxes,
  IconChevron,
  IconLink,
  IconPencil,
  IconPlus,
  IconRefresh,
  IconStar,
} from "@/icons";
import { Notice } from "@/ui";
import { useCallback, useEffect, useState } from "react";

export default function Providers({ apiBase }: { apiBase: string }) {
  const c = useChannelCopy();
  const t = useT();
  const [selected, setSelected] = useState<string | null>(() =>
    new URLSearchParams(window.location.hash.split("?")[1]).get("provider"),
  );
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [pending, setPending] = useState<"save" | "probe" | "directory" | "remove" | null>(null);
  const busy = pending !== null;
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback((signal: AbortSignal) => readChannels(apiBase, signal), [apiBase]);
  const resource = useDataSurface("channels:" + apiBase, [apiBase], load, {
    isEmpty: (value) => value.channels.length === 0,
  });
  const presets = useDataSurface(
    "services:" + apiBase,
    [apiBase],
    (signal) =>
      fetch(`${apiBase}/api/services`, { signal }).then(
        readJsonOrThrow<{ providers: ServicePreset[] }>,
      ),
    { isEmpty: () => false },
  );
  const models = useDataSurface(
    `channel-models:${apiBase}`,
    [apiBase, resource.data?.revision],
    (signal) =>
      fetch(`${apiBase}/api/models`, { signal }).then(
        readJsonOrThrow<Array<{ provider: string; id: string }>>,
      ),
    { isEmpty: () => false },
  );
  useEffect(() => {
    const sync = () =>
      setSelected(new URLSearchParams(window.location.hash.split("?")[1]).get("provider"));
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const selectedId = selected ?? resource.data?.channels[0]?.channel.id;
  const detail = resource.data?.channels.find((value) => value.channel.id === selectedId);
  const channelModels = models.data?.filter((model) => model.provider === detail?.channel.id);
  const reportApplied = (
    message: string,
    application?: ApplicationReceipt,
    warningCopy = t("integrations.catalogSyncFailed"),
  ) => {
    const warning = clientApplicationWarning(application, warningCopy);
    setNotice({
      ok: !warning,
      text: warning
        ? `${message} — ${warning}`
        : application?.status === "pending"
          ? c("pendingApply")
          : message,
    });
  };
  const run = async (
    action: () => Promise<unknown>,
    message: string | null = c("saved"),
    operation: typeof pending = "save",
  ) => {
    setPending(operation);
    setNotice(null);
    try {
      const result = (await action()) as
        | {
            ok?: boolean;
            applicable?: boolean;
            error?: string;
            discovery?: ChannelDetail["discovery"];
            application?: ApplicationReceipt;
            catalogRefresh?: ApplicationReceipt;
          }
        | undefined;
      resource.refresh();
      models.refresh();
      if (result?.ok === false)
        throw new Error(result.error || result.discovery?.error?.message || c("error"));
      if (result?.applicable === false) throw new Error(c("staticDirectory"));
      const application = result?.application ?? result?.catalogRefresh;
      if (message !== null) {
        reportApplied(
          message,
          application,
          operation === "probe" ? t("integrations.catalogRefreshSyncFailed") : undefined,
        );
      } else {
        const warning = clientApplicationWarning(
          application,
          t("integrations.catalogRefreshSyncFailed"),
        );
        if (warning) setNotice({ ok: false, text: warning });
      }
    } catch (cause) {
      setNotice({ ok: false, text: cause instanceof Error ? cause.message : c("error") });
    } finally {
      setPending(null);
    }
  };
  const saved = (id: string, application?: ApplicationReceipt) => {
    setSelected(id);
    setEditing(false);
    setAdding(false);
    resource.refresh();
    reportApplied(c("saved"), application);
  };
  const remove = async (target: ChannelDetail) => {
    if (
      !(await confirmAction({
        message: c("removeConfirm"),
        confirmLabel: c("remove"),
        tone: "danger",
      }))
    )
      return;
    await run(
      async () => {
        const result = await fetch(
          `${apiBase}/api/channels/${encodeURIComponent(target.channel.id)}?revision=${target.revision}`,
          { method: "DELETE" },
        ).then(readJsonOrThrow);
        setEditing(false);
        setSelected(null);
        return result;
      },
      c("saved"),
      "remove",
    );
  };
  return (
    <div className="channels-page">
      <PageHeader
        title={c("title")}
        actions={
          <>
            <Button
              aria-busy={resource.state.refreshing}
              disabled={resource.state.refreshing}
              onClick={() => resource.refresh()}
            >
              <IconRefresh aria-hidden="true" />
              {c("refresh")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                setAdding(true);
                setEditing(false);
              }}
            >
              <IconPlus aria-hidden="true" />
              {c("add")}
            </Button>
          </>
        }
      />
      {notice && <Notice tone={notice.ok ? "ok" : "err"}>{notice.text}</Notice>}
      {resource.state.showError && (
        <Notice tone="err">
          {c("error")}{" "}
          <Button size="sm" onClick={() => resource.refresh()}>
            {c("refresh")}
          </Button>
        </Notice>
      )}
      {resource.state.showSkeleton && <DataSurfaceSkeleton rows={4} label={c("loading")} />}
      {!resource.state.showSkeleton && (
        <div className="channels-layout">
          <nav className="channel-list" aria-label={c("title")}>
            <div className="channel-list-heading">
              {c("title")} <span>{resource.data?.channels.length ?? 0}</span>
            </div>
            {resource.data?.channels.map(({ channel, credential }) => {
              const isDefault = resource.data?.default_channel_id === channel.id;
              const tone = channel.disabled
                ? "quiet"
                : credential.state === "configured"
                  ? "ready"
                  : credential.state === "unknown"
                    ? "quiet"
                    : "attention";
              return (
                <button
                  key={channel.id}
                  type="button"
                  className="channel-list-item"
                  aria-current={selectedId === channel.id ? "true" : undefined}
                  onClick={() => {
                    setSelected(channel.id);
                    setEditing(false);
                    setAdding(false);
                  }}
                >
                  <ProviderMark
                    name={channel.service_id === "custom" ? channel.id : channel.service_id}
                    className="channel-list-mark"
                    size="md"
                  />
                  <span className="channel-list-label">
                    <strong>{channel.name || channel.id}</strong>
                    <small className="channel-state" data-tone={tone}>
                      {channel.disabled
                        ? t("models.tipDisabled")
                        : c(
                            credential.state === "configured"
                              ? "configured"
                              : credential.state === "missing"
                                ? "missing"
                                : credential.state === "reauth_required"
                                  ? "reauth"
                                  : "unknown",
                          )}
                    </small>
                  </span>
                  {isDefault && (
                    <span
                      title={c("default")}
                      aria-label={c("default")}
                      className="channel-default-mark"
                    >
                      <IconStar aria-hidden="true" />
                    </span>
                  )}
                </button>
              );
            })}
            {resource.data?.channels.length === 0 && <p className="muted">{c("empty")}</p>}
          </nav>
          <div className="channel-detail">
            {adding ? (
              <ChannelEditor
                apiBase={apiBase}
                presets={presets.data?.providers ?? []}
                onSaved={saved}
                onClose={() => setAdding(false)}
              />
            ) : detail ? (
              <>
                <div className="channel-identity">
                  <ProviderMark
                    name={
                      detail.channel.service_id === "custom"
                        ? detail.channel.id
                        : detail.channel.service_id
                    }
                    className="channel-identity-mark"
                    size="lg"
                  />
                  <div className="channel-identity-name">
                    <h2>{detail.channel.name || detail.channel.id}</h2>
                    <code>{detail.channel.id}</code>
                  </div>
                  <div className="channel-actions">
                    {resource.data?.default_channel_id === detail.channel.id ? (
                      <span className="channel-default-tag">
                        <IconStar aria-hidden="true" />
                        {c("default")}
                      </span>
                    ) : (
                      !editing && (
                        <Button
                          size="sm"
                          disabled={busy}
                          onClick={() =>
                            void run(() =>
                              setDefaultChannel(
                                apiBase,
                                detail.channel.id,
                                resource.data?.revision,
                              ).then(readJsonOrThrow),
                            )
                          }
                        >
                          {c("makeDefault")}
                        </Button>
                      )
                    )}
                    {!editing && (
                      <Button size="sm" disabled={busy} onClick={() => setEditing(true)}>
                        <IconPencil aria-hidden="true" />
                        {c("edit")}
                      </Button>
                    )}
                  </div>
                </div>
                {editing ? (
                  <ChannelEditor
                    key={`${detail.channel.id}:${detail.revision}`}
                    apiBase={apiBase}
                    detail={detail}
                    presets={presets.data?.providers ?? []}
                    onSaved={saved}
                    onClose={() => setEditing(false)}
                    onRemove={() => void remove(detail)}
                    removing={pending === "remove"}
                  />
                ) : (
                  <>
                    <div className="channel-overview">
                      <section className="channel-panel">
                        <h3 className="channel-section-title">
                          <IconLink aria-hidden="true" />
                          {c("connection")}
                        </h3>
                        <dl className="channel-facts">
                          <dt>{c("protocol")}</dt>
                          <dd>{detail.channel.protocol}</dd>
                          <dt>{c("baseUrl")}</dt>
                          <dd>
                            <code>{detail.channel.endpoints.base_url}</code>
                          </dd>
                        </dl>
                        {detail.channel.auth_mode === "key" &&
                          detail.credential.state !== "missing" && (
                            <CredentialLabel
                              key={detail.channel.id}
                              apiBase={apiBase}
                              channelId={detail.channel.id}
                              label={detail.credential.label}
                              onSaved={resource.refresh}
                            />
                          )}
                        <div className="channel-actions">
                          <Button
                            size="sm"
                            aria-busy={pending === "probe"}
                            disabled={busy || detail.channel.models?.live === false}
                            onClick={() =>
                              void run(
                                () =>
                                  fetch(
                                    `${apiBase}/api/channels/${encodeURIComponent(detail.channel.id)}/probe`,
                                    { method: "POST" },
                                  ).then(readJsonOrThrow),
                                c("checked"),
                                "probe",
                              )
                            }
                          >
                            {c("test")}
                          </Button>
                        </div>
                      </section>
                      <section className="channel-panel">
                        <h3 className="channel-section-title">
                          <IconBoxes aria-hidden="true" />
                          {c("models")}
                          {channelModels && (
                            <span className="channel-count">{channelModels.length}</span>
                          )}
                        </h3>
                        <dl className="channel-facts">
                          <dt>{c("defaultModel")}</dt>
                          <dd>{detail.channel.models?.default ?? c("inherit")}</dd>
                          <dt>{c("discovery")}</dt>
                          <dd>
                            {detail.channel.models?.live === false
                              ? c("staticDirectory")
                              : detail.discovery.state === "unsupported"
                                ? c("unsupported")
                                : detail.discovery.state === "refreshing"
                                  ? c("refreshing")
                                  : detail.discovery?.last_success_at
                                    ? `${c("discovered")} · ${new Date(detail.discovery.last_success_at).toLocaleString()}`
                                    : c("never")}
                          </dd>
                        </dl>
                        <div className="channel-actions">
                          <Button
                            size="sm"
                            aria-busy={pending === "directory"}
                            disabled={busy}
                            onClick={() =>
                              void run(
                                () => refreshChannelModels(apiBase, detail.channel.id, c("error")),
                                null,
                                "directory",
                              )
                            }
                          >
                            <IconRefresh aria-hidden="true" />
                            {c("refreshModels")}
                          </Button>
                          <Button
                            size="sm"
                            onClick={() =>
                              navigateHash(
                                "models?provider=" + encodeURIComponent(detail.channel.id),
                              )
                            }
                          >
                            {c("openModels")}
                          </Button>
                        </div>
                        {detail.discovery?.error && (
                          <Notice tone="warn">{detail.discovery.error.message}</Notice>
                        )}
                      </section>
                    </div>
                    {detail.channel.auth_mode === "oauth" && (
                      <AccountPanel
                        key={detail.channel.id}
                        apiBase={apiBase}
                        onChanged={resource.refresh}
                      />
                    )}
                    <details
                      className="channel-panel channel-diagnostics"
                      key={`diagnostics:${detail.channel.id}`}
                    >
                      <summary>
                        <IconChevron aria-hidden="true" />
                        {t("pws.completion.title")}
                      </summary>
                      <ProviderCompletionTest
                        apiBase={apiBase}
                        provider={detail.channel.id}
                        probeKey={String(detail.revision)}
                        models={(channelModels ?? []).map((model) => model.id)}
                      />
                    </details>
                    <PlannedFeature compact title={c("keyLimits")} description={c("pacing")} />
                  </>
                )}
              </>
            ) : (
              !resource.state.showSkeleton && <p className="muted">{c("empty")}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
