/** A bounded, observational client label. Header values themselves are never returned. */
export const USAGE_CLIENTS = [
  "codex_cli",
  "codex_desktop",
  "codex_ide",
  "claude_code",
  "claude_desktop",
  "grok",
  "opencode",
  "aside",
  "other",
  "unknown",
] as const;

export type UsageClient = (typeof USAGE_CLIENTS)[number];

const KNOWN_CLIENTS: ReadonlySet<string> = new Set(USAGE_CLIENTS);

/** Preserve absence on legacy rows; a present invalid value is explicitly unknown. */
export function normalizeRecordedClient(value: unknown): UsageClient | undefined {
  if (value === undefined) return undefined;
  return typeof value === "string" && KNOWN_CLIENTS.has(value) ? (value as UsageClient) : "unknown";
}
