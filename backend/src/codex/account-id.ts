/** Canonical persisted Codex pool-account id format. */
export const CODEX_ACCOUNT_ID_RE = /^[A-Za-z0-9._-]{1,64}$/;

// Account credentials are persisted in JSON object maps. These names have
// special meaning on ordinary JavaScript objects and must never be admitted as
// user-controlled keys.
const RESERVED_CODEX_ACCOUNT_IDS = new Set(["__proto__", "prototype", "constructor"]);

/**
 * Stable internal id under which the Codex Desktop login participates in account rotation.
 * It is reserved and must never be used as a public namespace target or pool-account id.
 */
export const MAIN_CODEX_ACCOUNT_ID = "__main__";
/** Internal result for an explicitly signed-out channel, never a stored account id. */
export const SIGNED_OUT_CODEX_ACCOUNT_ID = "__signed_out__";

export function isValidCodexAccountId(accountId: unknown): accountId is string {
  return typeof accountId === "string"
    && accountId !== MAIN_CODEX_ACCOUNT_ID
    && accountId !== SIGNED_OUT_CODEX_ACCOUNT_ID
    && CODEX_ACCOUNT_ID_RE.test(accountId)
    && !RESERVED_CODEX_ACCOUNT_IDS.has(accountId.toLowerCase());
}

type CodexAccountIdentityRow = { id: string; isMain: boolean };

/** Legacy invalid rows remain loadable for cleanup, but never participate in routing. */
export function isSelectableCodexPoolAccount(account: CodexAccountIdentityRow): boolean {
  return !account.isMain && isValidCodexAccountId(account.id);
}

/** A pre-validation pool row that collides with the internal Desktop-account sentinel. */
export function hasLegacyMainCodexPoolAccount(
  accounts: readonly CodexAccountIdentityRow[] | undefined,
): boolean {
  return accounts?.some(account => !account.isMain && account.id === MAIN_CODEX_ACCOUNT_ID) ?? false;
}

/** The configured channel identity, including a legacy single managed login. */
export function singleCodexAccountId(config: { codexSignedOut?: boolean; activeCodexAccountId?: string; codexAccounts?: readonly { id: string; isMain: boolean }[]; providers?: { openai?: { codexAccountMode?: string } } }): string {
  if (config.codexSignedOut === true) return SIGNED_OUT_CODEX_ACCOUNT_ID;
  if (config.providers?.openai?.codexAccountMode === "direct") return MAIN_CODEX_ACCOUNT_ID;
  if (config.activeCodexAccountId !== undefined) return config.activeCodexAccountId;
  const managed = (config.codexAccounts ?? []).filter(isSelectableCodexPoolAccount);
  return managed.length === 1 ? managed[0]!.id : MAIN_CODEX_ACCOUNT_ID;
}
