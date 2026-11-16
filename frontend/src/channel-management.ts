import type { ChannelDetail, SaveChannel } from "@/contracts/channels";
import { readJsonOrThrow } from "./fetch-json";

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
