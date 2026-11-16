import { normalizeHashPath, splitHashQuery } from "./hash-routing";

export type Page =
  | "home"
  | "integrations"
  | "providers"
  | "models"
  | "subagents"
  | "usage"
  | "logs"
  | "storage"
  | "dashboard";
export const VALID_PAGES = new Set<Page>([
  "home",
  "integrations",
  "providers",
  "models",
  "subagents",
  "usage",
  "logs",
  "storage",
  "dashboard",
]);
export const MODELS_TAB_HASHES = ["models/routing"] as const;
export const INTEGRATION_TAB_HASHES = ["integrations/codex", "integrations/dsh"] as const;
export const QUERY_HASH_PATHS: readonly string[] = ["providers", "models", "subagents"];
export function readPageFromHash(hash?: string): Page {
  const { path } = splitHashQuery(normalizeHashPath(hash ?? window.location.hash));
  const page = path.split("/")[0] as Page;
  return VALID_PAGES.has(page) ? page : "home";
}
export function hashBelongsToPage(path: string, page: Page): boolean {
  return (
    path === page ||
    (page === "logs" && path === "logs/debug") ||
    (page === "models" && (MODELS_TAB_HASHES as readonly string[]).includes(path)) ||
    (page === "integrations" && (INTEGRATION_TAB_HASHES as readonly string[]).includes(path))
  );
}
export type AppHashChangeAction = { page: Page; replaceTo: string | null };
export function resolveAppHashChange(rawHash: string): AppHashChangeAction {
  const { path, query } = splitHashQuery(normalizeHashPath(rawHash));
  const page = readPageFromHash(path);
  if (!hashBelongsToPage(path, page)) return { page, replaceTo: page };
  return { page, replaceTo: query && !QUERY_HASH_PATHS.includes(path) ? path : null };
}
export function canonicalHashPath(hash: string): string {
  const raw = normalizeHashPath(hash);
  return resolveAppHashChange(raw).replaceTo ?? raw;
}
