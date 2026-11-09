import type { RawEntry } from "./parsing";
import { CODEX_NATIVE_ALIAS_CATALOG_KIND } from "./kinds";

export const ROUTED_DESCRIPTION_PREFIX = "Routed via opencodex → ";
export const ROUTED_ORIGIN_FIELD = "opencodex_routed_origin";

export function hasComboRoutedOriginMarker(entry: RawEntry): boolean {
  return entry[ROUTED_ORIGIN_FIELD] === "combo-v1";
}

export function hasOcxRoutedAuthorship(entry: RawEntry): boolean {
  return hasComboRoutedOriginMarker(entry)
    || (typeof entry.description === "string" && entry.description.startsWith(ROUTED_DESCRIPTION_PREFIX));
}

/** Identify legacy virtual rows so a catalog refresh can remove them and restore shadowed entries. */
export function isRetiredComboCatalogEntry(entry: RawEntry, hasPhysicalProvider = false): boolean {
  return entry.opencodex_catalog_kind === CODEX_NATIVE_ALIAS_CATALOG_KIND
    || hasComboRoutedOriginMarker(entry)
    || (!hasPhysicalProvider && entry.owned_by === "combo" && hasOcxRoutedAuthorship(entry));
}

export function applyRoutedDescription(entry: RawEntry, generated: string): void {
  entry.description = generated;
  delete entry[ROUTED_ORIGIN_FIELD];
}
