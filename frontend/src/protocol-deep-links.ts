import { navigateHash } from "./hash-routing";
export function providerAccountsHash(provider: string): string {
  return "providers?" + new URLSearchParams({ provider }).toString();
}
export function openProviderAccounts(provider: string): void {
  navigateHash(providerAccountsHash(provider));
}
