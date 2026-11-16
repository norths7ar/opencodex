// Keep old Anthropic OAuth account identities private when reading historical rows.
type AnthropicInstanceRow = {
  adapter?: string;
  authMode?: string;
  baseUrl?: string;
  anthropicOAuthInstance?: unknown;
};

/**
 * Whether a configured provider row may be treated as the builtin instance of that name.
 *
 * `anthropic` keeps its historical meaning: the row named `anthropic` is the instance. A row named
 * `anthropic2` may predate the builtin as a user's custom provider, so it is the builtin only when it is
 * explicitly marked as an Anthropic OAuth instance. Transport endpoints never establish ownership.
 * Anything else keeps its custom meaning and is never pinned, enriched or routed as the builtin pool.
 */
export function isLegacyAnthropicAccountRow(
  name: string,
  row: AnthropicInstanceRow | undefined,
): boolean {
  if (name === "anthropic") return true;
  if (name !== "anthropic2" || !row) return false;
  return (
    Object.hasOwn(row, "anthropicOAuthInstance") &&
    row.anthropicOAuthInstance === "anthropic2" &&
    row.adapter === "anthropic" &&
    row.authMode === "oauth"
  );
}
