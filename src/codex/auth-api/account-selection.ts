import type { OcxConfig } from "../../types";
import { jsonResponse } from "./http";

/** Compatibility response for callers of the removed account-pool selector. */
export function persistCodexAccountSelection(config: OcxConfig, accountId: string | null, expectedPinnedAccountId?: string): Response {
  return jsonResponse({ error: "Account switching is not supported; sign in to replace this channel's account" }, 410);
}
