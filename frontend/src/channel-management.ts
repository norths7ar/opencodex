import type { ChannelDetail, ChannelView, SaveChannel } from "@/contracts/channels";
import type { ProviderUpdatePatch } from "./components/provider-workspace/types";
import { readJsonOrThrow } from "./fetch-json";
import type { ProvidersConfig } from "./pages/providers-shared";
import { providerServiceDisplayName } from "./provider-icons";

const REVISION_HEADER = "X-Config-Revision";

function channelPath(apiBase: string, id: string): string {
  return apiBase + "/api/channels/" + encodeURIComponent(id);
}

function revision(response: Response): number {
  const raw = response.headers.get(REVISION_HEADER);
  if (raw === null || !/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new Error("Channel API did not return a configuration revision.");
  }
  return Number(raw);
}

/** The configuration endpoint still supplies global settings and discovery data for the view. */
export async function loadProviderWorkspace(apiBase: string): Promise<ProvidersConfig> {
  for (let attempt = 0; attempt < 2; attempt++) {
    const [channelResponse, configResponse] = await Promise.all([
      fetch(apiBase + "/api/channels"),
      fetch(apiBase + "/api/config"),
    ]);
    const views = await readJsonOrThrow<ChannelView[]>(channelResponse);
    const config = await readJsonOrThrow<ProvidersConfig>(configResponse);
    if (!Array.isArray(views) || !config) throw new Error("Missing channel configuration.");
    if (revision(channelResponse) !== config.revision) continue;
    return {
      ...config,
      providers: Object.fromEntries(
        views.map((view) => [
          view.id,
          {
            ...config.providers[view.id],
            displayName:
              view.name && view.name !== view.id
                ? view.name
                : providerServiceDisplayName(view.service_id),
            serviceId: view.service_id,
            adapter: view.protocol,
            authMode: view.auth_mode,
            baseUrl: view.endpoints.base_url,
            disabled: view.disabled,
            hasApiKey: view.auth_mode === "key" && view.credential.state === "configured",
            hasHeaders: view.has_custom_headers,
            credentialState: view.credential.state,
          },
        ]),
      ),
    };
  }
  throw new Error("Channel configuration changed while it was being loaded.");
}

export async function readChannel(
  apiBase: string,
  id: string,
): Promise<{ detail: ChannelDetail; revision: number }> {
  const response = await fetch(channelPath(apiBase, id));
  const detail = await readJsonOrThrow<ChannelDetail>(response);
  if (!detail || detail.channel.id !== id) throw new Error("Unexpected channel detail.");
  return { detail, revision: revision(response) };
}

export function createChannel(apiBase: string, edit: SaveChannel): Promise<Response> {
  return fetch(apiBase + "/api/channels", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(edit),
  });
}

export async function updateChannel(
  apiBase: string,
  id: string,
  change: (detail: ChannelDetail) => SaveChannel,
): Promise<Response> {
  const current = await readChannel(apiBase, id);
  return fetch(channelPath(apiBase, id) + "?revision=" + current.revision, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(change(current.detail)),
  });
}

export function removeChannel(apiBase: string, id: string, expectedRevision?: number) {
  return fetch(
    channelPath(apiBase, id) +
      (expectedRevision === undefined ? "" : "?revision=" + expectedRevision),
    { method: "DELETE" },
  );
}

export const CHANNEL_PROTOCOLS = ["openai-chat", "openai-responses", "anthropic"] as const;

export function channelProtocol(value: string) {
  const protocol = CHANNEL_PROTOCOLS.find((option) => option === value);
  if (!protocol) throw new Error("Select a supported upstream protocol.");
  return protocol;
}

export function editChannel(detail: ChannelDetail, patch: ProviderUpdatePatch): SaveChannel {
  const channel = structuredClone(detail.channel);
  if (patch.adapter !== undefined) channel.protocol = channelProtocol(patch.adapter);
  if (patch.baseUrl !== undefined) channel.endpoints.base_url = patch.baseUrl;
  if (patch.apiKeyTransport !== undefined)
    channel.api_key_transport = patch.apiKeyTransport || null;
  if (patch.authMode === "key" || patch.authMode === "local") {
    channel.auth_mode = patch.authMode;
    channel.oauth_binding = null;
  }
  if (patch.allowPrivateNetwork !== undefined)
    channel.allow_private_network = patch.allowPrivateNetwork;
  if (patch.preserveResponsesReasoningContent !== undefined)
    channel.preserve_responses_reasoning_content = patch.preserveResponsesReasoningContent;
  if (patch.requestPacing !== undefined) {
    const pacing = patch.requestPacing;
    if (pacing?.keySelection === "round-robin")
      throw new Error("This channel supports one credential.");
    channel.request_pacing = pacing
      ? { ...pacing, enabled: pacing.enabled ?? true, keySelection: pacing.keySelection }
      : null;
  }
  if (patch.disabled !== undefined) channel.disabled = patch.disabled;
  if (Object.hasOwn(patch, "note")) channel.note = patch.note || null;
  if (patch.defaultModel !== undefined || patch.liveModels !== undefined) {
    channel.models = {
      ...channel.models,
      ...(patch.defaultModel === undefined ? {} : { default: patch.defaultModel || null }),
      ...(patch.liveModels === undefined ? {} : { live: patch.liveModels }),
    };
  }
  if (patch.modelDiscoveryClientVersion !== undefined) {
    channel.request_policy = {
      ...channel.request_policy,
      modelDiscoveryClientVersion: patch.modelDiscoveryClientVersion || null,
    };
  }
  const credential: SaveChannel["credential"] = patch.apiKey?.trim()
    ? { action: "set_key", value: patch.apiKey.trim() }
    : { action: "keep" };
  return { channel, credential };
}
