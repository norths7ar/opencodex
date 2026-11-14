/**
 * Models tab identity and hash mapping.
 *
 * Mirrors `logs-tab-keydown.ts`: the hash is the source of truth, so refresh, bookmark,
 * and Back/Forward all keep the tab choice. Kept out of `Models.tsx` because that file
 * is already large and because the tests want to import this directly.
 */

import { navigateHash, normalizeHashPath, splitHashQuery } from "../hash-routing";

/**
 * `catalog` rather than `models` for the first tab: the page is Models and its first
 * tab shows the plain model list, so a distinct id keeps "the page" and "the tab" from
 * ever having to be disambiguated in code. The visible label is still "Models".
 */
export type ModelsTab = "catalog" | "routing";

export const MODELS_TABS: readonly ModelsTab[] = ["catalog", "routing"];

export function modelsTabHash(tab: ModelsTab): string {
  return tab === "catalog" ? "models" : `models/${tab}`;
}

export function readModelsTab(hash = window.location.hash): ModelsTab {
  // A compatibility prefilter rides in `?query` (protocol-deep-links.ts); the tab is the path.
  const raw = splitHashQuery(normalizeHashPath(hash)).path;
  if (raw === "models/routing" || raw === "routing" || raw.startsWith("routing/")) return "routing";
  return "catalog";
}

/** Deliberate navigation: pushes a history entry so Back/Forward restore the tab. */
export function selectModelsTab(next: ModelsTab): void {
  navigateHash(modelsTabHash(next));
}

export function modelsTabDomId(tab: ModelsTab): string {
  return `models-tab-${tab}`;
}

export function modelsPanelDomId(tab: ModelsTab): string {
  return `models-panel-${tab}`;
}
