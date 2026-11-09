import { createHash } from "node:crypto";
import type { OcxConfig } from "../types";

const MULTI_AGENT_MODES = ["v1", "default", "v2"] as const;

/** Hash the local Codex integration settings without exposing their values in status output. */
export function appliedCodexSettingsDigest(config: OcxConfig): string {
  const mode = config.multiAgentMode;
  const model = config.injectionModel?.trim();
  // Preserve the order and version of the published digest.
  const payload = JSON.stringify({
    version: 1,
    settings: {
      fastMode: typeof config.fastMode === "boolean" ? config.fastMode : null,
      multiAgentMode: mode && (MULTI_AGENT_MODES as readonly string[]).includes(mode) ? mode : null,
      webSearchSidecarEnabled: config.webSearchSidecar?.enabled !== false,
      subagentDefaults: config.syncCodexSubagentDefaults === true && model
        ? { model, reasoningEffort: config.injectionEffort?.trim() || null }
        : null,
    },
  });
  return createHash("sha256").update(payload, "utf8").digest("hex");
}
