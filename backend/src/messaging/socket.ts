import { LocalMessagingError } from "./types";

export interface LocalSocket extends WebSocket { terminate(): void }

/** The upstream Unix control socket has no supported Windows transport. */
export function localDaemonEndpoint(codexHome: string, platform = process.platform): { url: string } {
  throw new LocalMessagingError("unsupported_socket", "The local Codex control socket is not supported on Windows.");
}

export function localSocket(url: string): LocalSocket {
  throw new LocalMessagingError("unsupported_socket", "The local Codex control socket is not supported on Windows.");
}
