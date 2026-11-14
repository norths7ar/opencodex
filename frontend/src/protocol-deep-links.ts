import { navigateHash, normalizeHashPath, splitHashQuery } from "./hash-routing";
import { canonicalHashPath } from "./app-routing";
export const PROVIDERS_HASH = "providers";

/** Longest provider name a deep link carries; the server bounds the same parameter at 200. */
const PROVIDER_NAME_LIMIT = 200;

function readQuery(hash: string, path: string): URLSearchParams | null {
  /*
   * Read the destination, not a legacy spelling the route hook is about to replace without
   * an event: a cold #claude/account must open Anthropic's Accounts tab on Providers.
   */
  const parts = splitHashQuery(canonicalHashPath(hash));
  return parts.path === path ? new URLSearchParams(parts.query) : null;
}

export function providerSettingsHash(provider: string): string {
  return `${PROVIDERS_HASH}?${new URLSearchParams({ provider }).toString()}`;
}

/** Which provider tab a providers link opens. Anything but an exact `tab=accounts` is Settings. */
export type ProviderDeepLinkTab = "settings" | "accounts";

/** The header quota strip links each provider chip to that provider's Accounts tab. */
export function providerAccountsHash(provider: string): string {
  return `${PROVIDERS_HASH}?${new URLSearchParams({ provider, tab: "accounts" }).toString()}`;
}

export function readProviderDeepLinkTab(hash: string = window.location.hash): ProviderDeepLinkTab {
  return readQuery(hash, PROVIDERS_HASH)?.get("tab") === "accounts" ? "accounts" : "settings";
}

/** The provider a providers hash names, or `null`. */
export function readProviderSettingsTarget(hash: string = window.location.hash): string | null {
  const name = readQuery(hash, PROVIDERS_HASH)?.get("provider")?.trim() ?? "";
  return name && name.length <= PROVIDER_NAME_LIMIT ? name : null;
}

export function openProviderSettings(provider: string): void {
  navigateHash(providerSettingsHash(provider));
}

/**
 * Open one provider's Accounts tab. Following the same link again (the user switched tabs after
 * arriving) leaves the hash unchanged, and an unchanged hash fires no `hashchange`, so announce
 * it by hand: the deep-link hook then re-applies the link and the Accounts tab opens again.
 */
export function openProviderAccounts(
  provider: string,
  win: Window & typeof globalThis = window,
): void {
  const target = providerAccountsHash(provider);
  if (normalizeHashPath(win.location.hash) === target) {
    win.dispatchEvent(new win.Event("hashchange"));
    return;
  }
  navigateHash(target, win);
}
