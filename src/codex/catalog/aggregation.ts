import { execFileSync } from "node:child_process";
import { collapseAntigravityPublicModels } from "../../providers/antigravity-effort-families";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, realpathSync } from "node:fs";
import { delimiter, dirname, join, resolve } from "node:path";
import { atomicWriteFile, expandUserPath, getConfigDir, websocketsEnabled } from "../../config";
import { CODEX_CONFIG_PATH, CODEX_MODELS_CACHE_PATH, DEFAULT_CATALOG_PATH, readRootTomlString, resolveCodexConfigPath } from "../paths";
import { clearModelCache, DEFAULT_MODEL_CACHE_TTL_MS, getFreshCached, getStaleCached, isModelsFetchCoolingDown, markModelsFetchFailure, setCached } from "../model-cache";
import { buildModelsRequest, resolveModelsAuthToken } from "../../oauth";
import type { OcxConfig, OcxProviderConfig } from "../../types";
import { modelInList } from "../../types";
import { CODEX_REASONING_LEVELS, codexEffortRank, configuredReasoningEfforts, modelRecordValue, resolveEffortAtOrBelow, sanitizeCodexReasoningEfforts } from "../../reasoning-effort";
import { getModelMetadata, getModelMetadataCaseInsensitive, listModelMetadata, resolveMetadataProvider } from "../../generated/model-metadata";
import { enrichProviderFromRegistry, shouldCaseFoldMetadataModelId } from "../../providers/derive";
import { getProviderRegistryEntry } from "../../providers/registry";
import { applyProviderContextCap, providerContextCap } from "../../providers/context-cap";
import { clampAutoCompactTokenLimit } from "../../providers/auto-compact-budget";
import { routedSlug, slugEquals, slugEquivalenceKey, slugsEquivalent } from "../../providers/slug-codec";
import { CODEX_GPT5_IDENTITY_LINE } from "../../adapters/identity";
import { filterCursorConfiguredModelsByLiveDiscovery } from "../../adapters/cursor/discovery";
import { fetchCursorUsableModels } from "../../adapters/cursor/live-models";
import { isCanonicalOpenAiForwardProvider, OPENAI_API_PROVIDER_ID, OPENAI_CODEX_PROVIDER_ID } from "../../providers/openai-tiers";
import { providerDestinationResolvedError } from "../../lib/destination-policy";
import { redactSecretString } from "../../lib/redact";
import upstreamModelsSnapshot from "../data/upstream-models.json";


import { catalogModelSlug } from "./parsing";
import type { CatalogModel, RawEntry } from "./parsing";

export const openAiApiCollisionWarnings = new Set<string>();

export function intersectStrings(values: readonly string[][]): string[] {
  if (values.length === 0) return [];
  const rest = values.slice(1).map(value => new Set(value));
  return [...new Set(values[0])].filter(value => rest.every(set => set.has(value)));
}

export function safeCatalogWarningLabel(value: string): string {
  return redactSecretString(value)
    .replace(/[\u0000-\u001f\u007f]/g, "?")
    .slice(0, 200);
}

/**
 * Keep the first row of each slug and drop the rest (#4730).
 *
 * First-win is the only answer that agrees with the ordering already decided upstream: the merge
 * ranks rows, so its first occurrence is the row it chose. Distinct slugs are never touched — an
 * alias row and the canonical routed row of the same provider model are two different public names
 * and both survive — and a row without a string slug passes through untouched.
 */
export function dedupeCatalogEntriesBySlug(models: RawEntry[]): RawEntry[] {
  const seen = new Set<string>();
  const out: RawEntry[] = [];
  for (const entry of models) {
    if (typeof entry.slug !== "string") {
      out.push(entry);
      continue;
    }
    if (seen.has(entry.slug)) continue;
    seen.add(entry.slug);
    out.push(entry);
  }
  return out;
}

/**
 * Every slug this proxy writes into the Codex catalog must appear exactly once (#4730).
 *
 * The cost of breaking it is the whole file: a slug-unique validating consumer refuses the catalog
 * outright, so one duplicated row takes every model with it. A 2.56.0 report carried 507 rows for
 * 72 unique slugs, every duplicate byte-identical, and Codex rejected the file as `source-invalid`.
 *
 * This is a write-boundary invariant rather than a repair of one producer, and that distinction is
 * deliberate: the reported catalog is evidence that some emit path can double a row, but nothing in
 * this tree has been shown to be that path, and a guard that only covered the producer someone
 * guessed at would leave the file corruptible by the next one. Both writers that serialize a merged
 * catalog call this as their LAST mutation — `writeRetainedCatalogSync` and the management
 * convergence commit — so uniqueness holds for the exact bytes that land on disk.
 *
 * Ordering is load-bearing. Running the guard before the effort clamp would be unsound:
 * `clampCatalogModelsToObservedCodexSupport` splices whole rows out when an exact-reserve ladder
 * clamps empty, so dropping a later same-slug row first can leave the slug with no row at all once
 * the surviving one is spliced.
 *
 * @param models - The finished row list, already clamped and finalized.
 * @param warn - Whether to report on `console.warn`. The convergence path merges under
 *   `warningPolicy: "suppress"` and stays silent for the same reason.
 * @returns The original array when it was already unique, so an unchanged catalog stays a no-op
 *   write; otherwise a first-win copy.
 */
export function enforceCatalogSlugUniqueness(models: RawEntry[], warn: boolean): RawEntry[] {
  const deduped = dedupeCatalogEntriesBySlug(models);
  if (deduped.length === models.length) return models;
  if (warn) {
    // A dropped row that differs from the kept one means two emit paths disagree about the same
    // slug's content. First-win still stands, but the operator needs to see WHICH slugs diverged
    // instead of silently losing data. The baseline is the row the dedupe actually keeps — the
    // FIRST occurrence — so the reported divergence is measured against what lands on disk.
    const keptBySlug = new Map<string, RawEntry>();
    for (const entry of models) {
      if (typeof entry.slug !== "string" || keptBySlug.has(entry.slug)) continue;
      keptBySlug.set(entry.slug, entry);
    }
    const divergentSlugs = new Set<string>();
    for (const entry of models) {
      if (typeof entry.slug !== "string") continue;
      const kept = keptBySlug.get(entry.slug);
      if (kept && kept !== entry && JSON.stringify(kept) !== JSON.stringify(entry)) {
        divergentSlugs.add(entry.slug);
      }
    }
    const divergentNote = divergentSlugs.size > 0
      ? `; divergent content on: ${[...divergentSlugs].slice(0, 5).map(safeCatalogWarningLabel).join(", ")}${divergentSlugs.size > 5 ? ", …" : ""}`
      : "";
    console.warn(
      `[opencodex] catalog sync dropped ${models.length - deduped.length} duplicate slug row(s), keeping the first occurrence of each slug (#4730)${divergentNote}.`,
    );
  }
  return deduped;
}

export function normalizedOpenAiApiSignature(model: CatalogModel): string {
  const normalized = {
    provider: model.provider,
    id: model.id,
    contextWindow: model.contextWindow ?? null,
    maxInputTokens: model.maxInputTokens ?? null,
    maxOutputTokens: model.maxOutputTokens ?? null,
    autoCompactTokenLimit: model.autoCompactTokenLimit ?? null,
    inputModalities: [...new Set(model.inputModalities ?? [])].sort(),
    reasoningEfforts: [...new Set(model.reasoningEfforts ?? [])].sort(),
    ownedBy: model.owned_by ?? null,
  };
  return JSON.stringify(normalized);
}

export function resetOpenAiApiCatalogWarningStateForTests(): void {
  openAiApiCollisionWarnings.clear();
}

export const slugAliasCollisionWarnings = new Set<string>();

export const accountSelectorShadowCollisionWarnings = new Set<string>();
let lastWarningReconciledGeneration = 0;

export function reconcileCatalogWarningMemos(generation: number): number {
  if (generation <= lastWarningReconciledGeneration) return 0;
  const removed = openAiApiCollisionWarnings.size
    + slugAliasCollisionWarnings.size
    + accountSelectorShadowCollisionWarnings.size;
  openAiApiCollisionWarnings.clear();
  slugAliasCollisionWarnings.clear();
  accountSelectorShadowCollisionWarnings.clear();
  lastWarningReconciledGeneration = generation;
  return removed;
}

/** Warn once when a live provider row loses its reserved slug to an account selector. */
export function warnAccountSelectorShadowedProviderOnce(slug: string): void {
  if (accountSelectorShadowCollisionWarnings.has(slug)) return;
  accountSelectorShadowCollisionWarnings.add(slug);
  console.warn(
    `[opencodex] account selector collision on "${safeCatalogWarningLabel(slug)}": the account-bound native model wins and the shadowed provider model is omitted from the catalog. Rename the provider or account selector.`,
  );
}

export function resolveSlugAliasCollisions(goModels: CatalogModel[]): Set<CatalogModel> {
  const skipped = new Set<CatalogModel>();
  const winnerByAlias = new Map<string, CatalogModel>();
  for (const m of goModels) {
    const key = catalogModelSlug(m);
    const winner = winnerByAlias.get(key);
    if (!winner) {
      winnerByAlias.set(key, m);
      continue;
    }
    const winnerIsPlainAlias = !winner.id.includes("/");
    const currentIsPlainAlias = !m.id.includes("/");
    if (currentIsPlainAlias && !winnerIsPlainAlias) {
      skipped.add(winner);
      winnerByAlias.set(key, m);
    } else {
      skipped.add(m);
    }
    if (!slugAliasCollisionWarnings.has(key)) {
      slugAliasCollisionWarnings.add(key);
      console.warn(
        `[opencodex] slug alias collision on "${key}": multiple native ids encode to the same Codex-facing slug; `
        + "the plain-hyphen native id is cataloged, the slash id remains callable via its raw selector.",
      );
    }
  }
  return skipped;
}

export function uniqueCatalogModelsForPublicList(goModels: CatalogModel[]): CatalogModel[] {
  goModels = collapseAntigravityPublicModels(goModels);
  const collisionSkipped = resolveSlugAliasCollisions(goModels);
  const seen = new Set<string>();
  const out: CatalogModel[] = [];
  for (const model of goModels) {
    if (collisionSkipped.has(model)) continue;
    const slug = catalogModelSlug(model);
    if (seen.has(slug)) continue;
    seen.add(slug);
    out.push(model);
  }
  return out;
}

export function uniqueCatalogModelsForRawPublicList(goModels: CatalogModel[]): CatalogModel[] {
  goModels = collapseAntigravityPublicModels(goModels);
  const publicId = (model: CatalogModel): string => model.alias ?? `${model.provider}/${model.id}`;
  const seen = new Set<string>();
  const out: CatalogModel[] = [];
  for (const model of goModels) {
    const id = publicId(model);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(model);
  }
  return out;
}
