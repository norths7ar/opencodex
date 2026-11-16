/** Pure hash → page resolution used by App route state. */

import { normalizeHashPath, splitHashQuery } from "./hash-routing";

export type Page =
  | "home"
  | "setup"
  | "advanced"
  | "dashboard"
  | "startup"
  | "providers"
  | "models"
  | "subagents"
  | "logs"
  | "usage"
  | "storage"
  | "codex-set"
  | "integrations"
  | "claude";

export const VALID_PAGES = new Set<Page>([
  "home",
  "setup",
  "advanced",
  "dashboard",
  "startup",
  "providers",
  "models",
  "subagents",
  "logs",
  "usage",
  "storage",
  "codex-set",
  "integrations",
  "claude",
]);

export function readPageFromHash(hash?: string): Page {
  const raw = splitHashQuery(
    normalizeHashPath(hash ?? (typeof window !== "undefined" ? window.location.hash : "")),
  ).path;
  // Sub-views use a "/" suffix (e.g. #logs/debug); the first segment is the page id.
  const pageId = raw.split("/")[0] as Page;
  // Legacy: Debug used to be a standalone page; it now lives as a tab on Logs.
  if (pageId === ("debug" as Page)) return "logs";
  // Retired page bookmarks open the closest supported Lite section.
  if (pageId === ("codex-auth" as Page) || pageId === "codex-set") return "providers";
  if (pageId === "advanced") return "dashboard";
  if (pageId === "setup" || pageId === "claude") return "integrations";
  if (pageId === ("routing" as Page)) return "models";
  if (pageId === ("api" as Page) || pageId === ("grok" as Page)) return "integrations";

  // No hash (or an unknown one) opens Home, the v2 landing page.
  return VALID_PAGES.has(pageId) ? pageId : "home";
}

/**
 * The hash a page will end up on once the resolver's passive rewrite lands. The route
 * hook applies that rewrite with replaceState, which emits no event, so a component that
 * reads the hash during the same mount (for example, Connect's tab strip) can read the
 * destination rather than the retired bookmark.
 */
export function canonicalHashPath(hash: string): string {
  const raw = normalizeHashPath(hash);
  return resolveAppHashChange(raw).replaceTo ?? raw;
}

/**
 * Dashboard section tabs live in the hash so refresh/bookmark/back-forward keep the
 * choice, mirroring Logs (`#logs` / `#logs/debug`). Overview is the bare `#dashboard`,
 * so it has no suffix entry here.
 */
export const DASHBOARD_TAB_HASHES = ["dashboard/providers", "dashboard/models"] as const;

/** Models keeps the catalog and explicit routing configuration. */
export const MODELS_TAB_HASHES = ["models/routing"] as const;

export const INTEGRATION_TAB_HASHES = ["integrations/codex", "integrations/dsh"] as const;

/** Preserve query parameters for provider settings links. */
export const QUERY_HASH_PATHS: readonly string[] = ["providers", "models", "subagents"];

/** Where the retired `#claude/account` bookmark lands: Anthropic's Accounts tab on Providers. */
export const CLAUDE_ACCOUNT_REDIRECT = "providers?provider=anthropic&tab=accounts";

export function hashBelongsToPage(rawHash: string, page: Page): boolean {
  return (
    rawHash === page ||
    (page === "claude" && ["claude/code", "claude/desktop"].includes(rawHash)) ||
    (page === "logs" && rawHash === "logs/debug") ||
    (page === "usage" && rawHash === "usage/companion") ||
    (page === "codex-set" && (rawHash === "codex-set/prompt" || rawHash === "codex-set/desktop")) ||
    (page === "models" && (MODELS_TAB_HASHES as readonly string[]).includes(rawHash)) ||
    (page === "dashboard" && (DASHBOARD_TAB_HASHES as readonly string[]).includes(rawHash)) ||
    (page === "integrations" && (INTEGRATION_TAB_HASHES as readonly string[]).includes(rawHash))
  );
}

/** Result of resolving an incoming hash. */
export type AppHashChangeAction = {
  page: Page;
  /** When non-null, passively replace the hash (no new history entry). */
  replaceTo: string | null;
};

/**
 * Resolve what App should do for the current location hash.
 * Any rewrite this returns is passive: callers apply it with replaceState, never a
 * push, so Back is never trapped on a hash the router immediately corrects.
 */
export function resolveAppHashChange(rawHash: string): AppHashChangeAction {
  const { path, query } = splitHashQuery(rawHash);
  if (query || path !== rawHash) {
    // Route on the path alone; keep the query only where a page owns it, so Back/Forward
    // restores it and an unrelated page never carries a stale one.
    const action = resolveAppHashChange(path);
    if (action.replaceTo === null && QUERY_HASH_PATHS.includes(path)) return action;
    return { page: action.page, replaceTo: action.replaceTo ?? path };
  }
  const nextPage = readPageFromHash(rawHash);

  // Legacy: Debug used to be a standalone page.
  if (rawHash === "debug" || rawHash.startsWith("debug/")) {
    return { page: "logs", replaceTo: "logs/debug" };
  }

  // ChatGPT account settings now live on Providers.
  if (/^(?:codex-auth|codex-set)(?:\/|$)/.test(rawHash)) {
    return { page: "providers", replaceTo: "providers" };
  }
  if (rawHash === "routing" || rawHash.startsWith("routing/")) {
    return { page: "models", replaceTo: "models/routing" };
  }

  /* Legacy top-level integration pages. */
  if (rawHash === "api") return { page: "integrations", replaceTo: "integrations" };
  if (/^claude(?:\/|$)/.test(rawHash)) return { page: "integrations", replaceTo: "integrations" };
  if (rawHash === "grok") return { page: "integrations", replaceTo: "integrations" };

  // Legacy deep link from the removed dual-layout era.
  if (rawHash === "providers/workspace") return { page: "providers", replaceTo: "providers" };

  // An unrecognised sub-hash is normalised away rather than left in the URL.
  if (!hashBelongsToPage(rawHash, nextPage)) return { page: nextPage, replaceTo: nextPage };
  return { page: nextPage, replaceTo: null };
}
