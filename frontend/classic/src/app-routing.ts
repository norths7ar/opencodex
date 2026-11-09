/** Pure hash → page resolution used by App route state. */

import { normalizeHashPath, splitHashQuery } from "./hash-routing";

export type Page =
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
  const raw = splitHashQuery(normalizeHashPath(
    hash ?? (typeof window !== "undefined" ? window.location.hash : ""),
  )).path;
  // Sub-views use a "/" suffix (e.g. #logs/debug); the first segment is the page id.
  const pageId = raw.split("/")[0] as Page;
  // Legacy: Debug used to be a standalone page; it now lives as a tab on Logs.
  if (pageId === ("debug" as Page)) return "logs";
  // Legacy: Codex Auth became the Multi-auth tab of Codex Set. #codex-auth is a
  // bookmarkable URL that has shipped, and use-app-route-state reads the initial
  // page straight from the hash, so without this an old bookmark lands on an
  // unknown page.
  if (pageId === ("codex-auth" as Page)) return "codex-set";
  if (pageId === ("routing" as Page)) return "models";
  // Legacy integration pages now live below one Integrations route. Returning
  // the destination page here keeps the initial hook state aligned until the
  // resolver replaces the hash with the exact nested destination.
  if (pageId === ("api" as Page)
    || pageId === ("grok" as Page)) return "integrations";
  if (raw === "integrations/claude" || raw === "integrations/claude/desktop") return "claude";
  // Claude accounts live on Providers now; the old Account sub-tab bookmark opens them there.
  if (raw === "claude/account") return "providers";
  return VALID_PAGES.has(pageId) ? pageId : "dashboard";
}

/**
 * The hash a page will end up on once the resolver's passive rewrite lands. The route
 * hook applies that rewrite with replaceState, which emits no event, so a component that
 * reads the hash during the same mount (Connect's tab strip, the Providers deep link) has
 * to read the destination rather than the legacy spelling: #integrations/claude/desktop
 * must select the Claude Desktop tab on a cold load, not Overview, and #claude/account
 * must open Anthropic's Accounts tab.
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


/**
 * Integrations uses a wrapping outer tab strip. Claude Desktop is a nested
 * route owned by the Claude family panel, but it still has to be registered
 * here or App normalization strips it before Claude can read it.
 */
export const INTEGRATION_TAB_HASHES = [
  "integrations/keys",
  "integrations/codex",
  "integrations/claude",
  "integrations/claude/desktop",
  "integrations/grok",
  "integrations/cursor",
  "integrations/opencode",
  "integrations/pi",
  "integrations/gentle-shell",
  "integrations/omp",
  "integrations/hermes",
  "integrations/openclaw",
  "integrations/kimi",
  "integrations/gajae",
  "integrations/dsh",
  "integrations/mcode",
  "integrations/zcode",
  "integrations/commandcode",
  "integrations/prime",
  "integrations/aside",
  "integrations/raycast",
  "integrations/omo",
  "integrations/cline",
  "integrations/kilo",
  "integrations/droid",
  "integrations/qoder",
  "integrations/qoder-cn",
  "integrations/workbuddy",
  "integrations/workbuddy-ai",
  "integrations/t3code",
] as const;

/** Preserve query parameters for provider settings links. */
export const QUERY_HASH_PATHS: readonly string[] = ["providers"];

/** Where the retired `#claude/account` bookmark lands: Anthropic's Accounts tab on Providers. */
export const CLAUDE_ACCOUNT_REDIRECT = "providers?provider=anthropic&tab=accounts";

export function hashBelongsToPage(rawHash: string, page: Page): boolean {
  return rawHash === page
    || (page === "claude" && ["claude/code", "claude/desktop"].includes(rawHash))
    || (page === "logs" && rawHash === "logs/debug")
    || (page === "codex-set" && (rawHash === "codex-set/prompt" || rawHash === "codex-set/desktop"))
    || (page === "models" && (
      (MODELS_TAB_HASHES as readonly string[]).includes(rawHash)
    ))
    || (page === "dashboard"
      && (DASHBOARD_TAB_HASHES as readonly string[]).includes(rawHash))
    || (page === "integrations"
      && (INTEGRATION_TAB_HASHES as readonly string[]).includes(rawHash));
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

  /* Legacy: Codex Auth is now the Multi-auth tab of Codex Set. */
  if (rawHash === "codex-auth" || rawHash.startsWith("codex-auth/")) {
    return { page: "codex-set", replaceTo: "codex-set" };
  }
  if (rawHash === "routing" || rawHash.startsWith("routing/")) {
    return { page: "models", replaceTo: "models/routing" };
  }

  /* Legacy top-level integration pages. */
  if (rawHash === "api") return { page: "integrations", replaceTo: "integrations/keys" };
  if (rawHash === "integrations/claude") return { page: "claude", replaceTo: "claude/code" };
  if (rawHash === "integrations/claude/desktop") return { page: "claude", replaceTo: "claude/desktop" };
  // The Claude page lost its Account sub-tab; Anthropic's Accounts tab on Providers replaces it.
  if (rawHash === "claude/account") return { page: "providers", replaceTo: CLAUDE_ACCOUNT_REDIRECT };
  // The read-only Settings sub-tab folded into Code, which already holds its controls.
  if (rawHash === "claude/settings") return { page: "claude", replaceTo: "claude/code" };
  if (rawHash === "grok") return { page: "integrations", replaceTo: "integrations/grok" };

  // Legacy deep link from the removed dual-layout era.
  if (rawHash === "providers/workspace") return { page: "providers", replaceTo: "providers" };

  // An unrecognised sub-hash is normalised away rather than left in the URL.
  if (!hashBelongsToPage(rawHash, nextPage)) return { page: nextPage, replaceTo: nextPage };
  return { page: nextPage, replaceTo: null };
}
