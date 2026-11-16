/** Closed report categories; an absent historical client is kept distinct. */
export const USAGE_APPS = [
  "codex",
  "claude_code",
  "claude_desktop",
  "grok",
  "opencode",
  "aside",
  "other",
  "unknown",
  "unclassified",
] as const;
export type UsageApp = (typeof USAGE_APPS)[number];
