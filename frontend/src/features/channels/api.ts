import type { ApplicationReceipt, ChannelDetail, ChannelDirectory } from "@/contracts/channels";
import { readJsonOrThrow } from "@/fetch-json";
import type { ProviderDiscoverySummary } from "@/models-groups";

export async function readChannels(
  apiBase: string,
  signal?: AbortSignal,
): Promise<ChannelDirectory> {
  const result = await fetch(`${apiBase}/api/channels`, { signal }).then(
    readJsonOrThrow<ChannelDirectory>,
  );
  if (!result || !Array.isArray(result.channels) || !Number.isInteger(result.revision)) {
    throw new Error("Invalid channel directory response.");
  }
  return result;
}

export async function refreshChannelModels(
  apiBase: string,
  provider: string,
  errorMessage: string,
) {
  const response = await fetch(
    `${apiBase}/api/channels/${encodeURIComponent(provider)}/models/refresh`,
    { method: "POST" },
  );
  return readJsonOrThrow<{
    ok: boolean;
    discovery: ChannelDetail["discovery"];
    catalogRefresh: ApplicationReceipt;
  }>(response, errorMessage);
}

export function discoveryView(detail: ChannelDetail): ProviderDiscoverySummary | undefined {
  const value = detail.discovery;
  if (!value) return undefined;
  const timestamps = {
    checkedAt: value.last_attempt_at ?? undefined,
    lastSuccessAt: value.last_success_at ?? undefined,
    error: value.error?.message,
    staleDefaultModel: value.stale_default_model ?? undefined,
    staleAliases: value.stale_aliases,
  };
  if (
    value.state === "not_checked" ||
    value.state === "unsupported" ||
    value.state === "refreshing"
  )
    return { status: value.state, ...timestamps };
  return value.state === "failed"
    ? // eslint-disable-next-line local-i18n/no-hardcoded-ui-strings -- Internal discovery classification, not a display label.
      { status: "failed", reason: "provider", ...timestamps }
    : { status: "ok", ...timestamps };
}

/** Display projection: configuration identity and null semantics stay in the transport DTO. */
export function channelView(detail: ChannelDetail) {
  const { channel, credential } = detail;
  return {
    name: channel.id,
    adapter: channel.protocol,
    serviceId: channel.service_id,
    displayName: channel.name,
    authMode: channel.auth_mode,
    baseUrl: channel.endpoints.base_url,
    disabled: channel.disabled,
    credentialState: credential.state,
    hasApiKey: credential.state === "configured",
    hasHeaders: detail.has_custom_headers,
    defaultModel: channel.models?.default ?? undefined,
    models: channel.models?.static ?? [],
    liveModels: channel.models?.live,
    apiKeyTransport: channel.api_key_transport ?? undefined,
    note: channel.note ?? undefined,
    allowPrivateNetwork: channel.allow_private_network,
    preserveResponsesReasoningContent: channel.preserve_responses_reasoning_content ?? undefined,
    modelDiscoveryClientVersion: channel.request_policy?.modelDiscoveryClientVersion ?? undefined,
    discovery: discoveryView(detail),
    contextWindow: channel.models?.metadata?.defaults?.contextWindow ?? undefined,
    modelContextWindows: Object.fromEntries(
      Object.entries(channel.models?.metadata?.overrides ?? {}).flatMap(([id, value]) =>
        typeof value.contextWindow === "number" ? [[id, value.contextWindow]] : [],
      ),
    ),
  };
}

export async function readChannelViews(apiBase: string, signal?: AbortSignal) {
  const directory = await readChannels(apiBase, signal);
  return directory.channels.map(channelView);
}

export function setDefaultChannel(apiBase: string, channelId: string, revision?: number) {
  return fetch(`${apiBase}/api/channels/default`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel_id: channelId, revision }),
  });
}
