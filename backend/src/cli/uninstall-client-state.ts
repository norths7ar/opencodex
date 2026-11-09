import { getConfigDir } from "../config";
import { assertClientLifecycleHeld, withClientLifecycle } from "../client/lifecycle-lock";
import { beginConfigOwnershipTeardown, removeOwnedConfigState, type ConfigRemovalResult } from "../lib/config-ownership";
import { windowsSecretAclReapPendingAtOrBelow } from "../lib/windows-secret-acl";
import { supportsDesktopPicker } from "../claude/desktop-picker-platform";
import { sharedTeardownAuthorized, type UninstallObservation } from "./uninstall-plan";
import { cleanupOwnedIntegrationsBeforeUninstall } from "./uninstall-integrations";

/** Outcome of retiring the Desktop picker profile and every picker root this install trusted. */
export interface PickerUninstallCleanup { ok: boolean; residual?: string[] }

export interface UninstallClientStateDeps {
  withLifecycle: typeof withClientLifecycle;
  cleanupIntegrations: typeof cleanupOwnedIntegrationsBeforeUninstall;
  remove: () => ConfigRemovalResult;
  /** True while a timed-out icacls child still owns a path at or below the config directory. */
  aclReapPending: (rootPath: string) => boolean;
  /** Release the picker profile, then untrust the current and any pending predecessor picker root. */
  removePickerArtifacts: () => Promise<PickerUninstallCleanup>;
}

/**
 * The picker root lives in the OS trust store (login keychain, CurrentUser Root on Windows), outside
 * the config directory. Deleting config first would erase ca.pem and the pending-untrust journal,
 * the only records naming which root to remove, and strand a trusted root after uninstall.
 */
async function removePickerArtifactsBeforeUninstall(): Promise<PickerUninstallCleanup> {
  if (!supportsDesktopPicker()) return { ok: true };
  const configDir = getConfigDir();
  const [{ removeDesktopPickerArtifacts }, { drainPendingPickerCaUntrust }] = await Promise.all([
    import("../claude/desktop-picker"),
    import("../claude/intercept/picker-ca-cleanup"),
  ]);
  const removed = await removeDesktopPickerArtifacts({ configDir });
  if (!removed.ok) return removed;
  return await drainPendingPickerCaUntrust(configDir) ? { ok: true } : { ok: false, residual: ["pending_trust"] };
}

const defaults: UninstallClientStateDeps = {
  withLifecycle: withClientLifecycle,
  cleanupIntegrations: cleanupOwnedIntegrationsBeforeUninstall,
  remove: () => removeOwnedConfigState(getConfigDir()),
  aclReapPending: rootPath => windowsSecretAclReapPendingAtOrBelow(rootPath),
  removePickerArtifacts: removePickerArtifactsBeforeUninstall,
};

/** Restore owned local integration artifacts before removing their recovery records. */
export async function removeOwnedConfigAfterDesktopCleanup(
  observed: UninstallObservation,
  deps: UninstallClientStateDeps = defaults,
): Promise<ConfigRemovalResult> {
  if (!sharedTeardownAuthorized(observed)) {
    throw new Error("Client cleanup refused: service or proxy teardown is not proven.");
  }
  // A rerun after an interrupted uninstall finds the tombstone; this process's own cleanup steps
  // (picker, integrations) still register state, which the remover below then removes.
  const endTeardown = beginConfigOwnershipTeardown();
  try {
    return await removeOwnedConfigInTeardown(deps);
  } finally {
    endTeardown();
  }
}

async function removeOwnedConfigInTeardown(deps: UninstallClientStateDeps): Promise<ConfigRemovalResult> {
  // Picker cleanup takes L itself, so it runs before the final lock. A residual keeps every
  // record the retry needs: the config directory still names the root to untrust.
  const picker = await deps.removePickerArtifacts();
  if (!picker.ok) {
    throw new Error(`Client cleanup refused: Claude Desktop picker cleanup left ${picker.residual?.join(", ") || "state"} behind; `
      + "approve the certificate removal prompt or run 'ocx claude desktop picker off', then rerun 'ocx uninstall'.");
  }

  // Exclude concurrent local Desktop writes until the ownership records are retired.
  return deps.withLifecycle(async held => {
    assertClientLifecycleHeld(held);
    // The async ACL belt releases its caller on a stalled `icacls.exe`, which is what keeps
    // startup and shutdown bounded. It is not evidence that the child released the directory,
    // and on Windows a live handle makes this removal fail partway instead of cleanly. Refuse
    // promptly and let the operator retry: waiting here would hand a stuck child the power to
    // hang `ocx uninstall`, which is the bound the belt exists to preserve.
    if (deps.aclReapPending(getConfigDir())) {
      throw new Error("Client cleanup refused: ACL hardening still owns a path under the config directory.");
    }
    // Integration records are the authority that permits removing only OpenCodex-owned fragments
    // from third-party files. Delete them only after every recorded contribution is retired.
    await deps.cleanupIntegrations();
    return deps.remove();
  });
}
