import { existsSync } from "node:fs";
import { getConfigDir } from "../config";
import { durableBunRuntime, type DurableBunRuntime } from "../lib/bun-runtime";
import { assertSelectedRuntimeWritable, type RuntimePreflightDeps } from "../lib/bun-runtime-preflight";
import { randomUUID } from "node:crypto";
import { defaultWinswEntry, installWinswService } from "../lib/winsw";
import { resolveWindowsTaskDiagnosticUserId, diagnoseService } from "./diagnostics";
import type { ServiceDiagnostic } from "./diagnostics";
import { assertServiceAuthEnvironment } from "./auth-preflight";
import { assertServiceEnvironmentMatchesInstall } from "./guards";
import { TASK, writeServiceInstallState, serviceSourceDir, resolveServiceOwnership, serviceLauncherPathDiagnostic } from "./state";
import type { ServiceOwnership, ServiceOwnershipResolution } from "./state";
import { writeWindowsSchedulerAssets, reregisterWindowsSchedulerTask, windowsSchedulerRegistrationMatchesSnapshot, restoreWindowsSchedulerTaskIfAbsent, startWindows, stopWindows, statusWindowsXml } from "./windows-ops";
import { probeWindowsSchedulerTask, SCHEDULER_SETTLE_DELAYS_MS, settleDelay } from "./windows-scheduler";
import type { WindowsSchedulerTaskProbe } from "./windows-scheduler";
import { taskXmlSection, taskXmlWithoutCommentsAndCdata, taskXmlElementCount, taskXmlOptionalValueEquals, windowsTaskRegistrationOwnedByAttempt, windowsTaskHasSessionRecoveryTriggers, windowsTaskRegistrationHealthy, windowsTaskRegistrationRefreshableLegacy } from "./windows-taskxml";
import type { ExpectedWindowsTaskUserId } from "./windows-taskxml";
import { win32 } from "node:path";

/**
 * The two CLI verbs `repairService` serves. They differ on ONE platform and ONE case: a
 * macOS job that is already loaded from the current plist, which `repair` must not touch and
 * `restart` must restart.
 */
export type ServiceRepairVerb = "repair" | "restart";

export interface RepairServiceDeps extends RuntimePreflightDeps {
  diagnose?: () => ServiceDiagnostic;
  assertEnv?: () => void;
  assertAuth?: () => void;
  writeSchedulerAssets?: (runtime: DurableBunRuntime) => void;
  stopScheduler?: () => void;
  startScheduler?: () => void;
  writeSchedulerState?: (runtime: DurableBunRuntime) => void;
  writeNativeState?: (runtime: DurableBunRuntime) => void;
  repairNative?: (runtime: DurableBunRuntime) => void | Promise<void>;
  repairSystemd?: () => void;
  /** Restarts a launchd job the install path deliberately left alone. `restart` only. */
  /** Resolves the recorded runtime owner across every state path. */
  readOwnership?: () => ServiceOwnershipResolution;
  /**
   * Which CLI verb is being served. `repair` must leave a healthy service alone — that no-op
   * IS the #4236 fix — while `restart` promises a new process, so on darwin it kicks the job
   * the no-op path did not touch. Windows (stop + start) and Linux
   * (`systemctl --user restart`) already restart unconditionally, so neither reads this.
   */
  verb?: ServiceRepairVerb;
  /** Reads live registered task XML; may be called again after failure, empty when unreadable. */
  readSchedulerXml?: () => string;
  /** Bounded wait before retrying an unreadable live registration snapshot. */
  settleSchedulerRead?: (delayMs: number) => void | Promise<void>;
  /** Proves fixed-name task presence when its live XML is empty or unreadable. */
  probeScheduler?: () => WindowsSchedulerTaskProbe;
  /** Re-registers the task from freshly staged XML. Used only when the definition is stale. */
  reregisterScheduler?: (attemptNonce: string, expectedExistingXml: string) => Promise<void>;
  /** Publishes the captured registration only when the fixed task name remains absent. */
  restoreSchedulerIfAbsent?: (registeredXml: string) => Promise<void>;
  /** Resolves the account the registered triggers must match; null when it cannot be resolved. */
  resolveExpectedUserId?: (registeredXml: string) => ExpectedWindowsTaskUserId | null;
  /** Exact scheduler action values used by validation; defaults to the installed paths. */
  schedulerWscript?: string;
  schedulerLauncher?: string;
  /** Test seam — defaults to process.platform so Linux CI cannot hit real installSystemd. */
  platform?: NodeJS.Platform;
  /** Test seam for a recorded temporary launcher. */
  launcherPathDiagnostic?: () => string | null;
}

/**
 * Why a repair stops when something other than this CLI owns the runtime.
 *
 * A legacy foreign ownership record prevents incidental activation of an existing service.
 * Only a deliberate service install takes ownership back.
 */
export function foreignServiceOwnerRefusal(ownership: ServiceOwnership, action = "repair"): string {
  return `Background service ${action} stopped: another installation owns the runtime `
    + `(install ${ownership.installId}, consent generation ${ownership.consentGeneration}).\n`
    + "The service registration was left exactly as it is — not re-enabled, not rewritten and not restarted.\n"
    + "Stop the owning installation before running 'ocx service install' to take ownership back.";
}

/**
 * Why an unreadable or contradictory record stops a repair too.
 *
 * Collapsing "I could not read the claim" into "there is no claim" is how a consented
 * takeover gets reactivated by a permissions error. `ocx service install` is deliberately
 * not gated, so this never leaves an operator without a way forward.
 */
export function unknownServiceOwnerRefusal(reason: string, action = "repair"): string {
  return `Background service ${action} stopped: ${reason}, so the runtime's recorded owner could `
    + "not be determined.\n"
    + "The service registration was left exactly as it is — not re-enabled, not rewritten and not restarted.\n"
    + "Repair the service-state file or its permissions, then run 'ocx service install' to take the runtime back.";
}

async function assertSchedulerSnapshotBeforeStart(
  readSchedulerXml: () => string,
  expectedXml: string,
  settle: (delayMs: number) => void | Promise<void>,
  changedMessage: string,
  unreadableMessage: string,
): Promise<void> {
  await assertSchedulerRegistrationBeforeStart(
    readSchedulerXml,
    settle,
    currentXml => windowsSchedulerRegistrationMatchesSnapshot(currentXml, expectedXml),
    changedMessage,
    unreadableMessage,
  );
}

export async function assertSchedulerRegistrationBeforeStart(
  readSchedulerXml: () => string,
  settle: (delayMs: number) => void | Promise<void>,
  matchesExpected: (currentXml: string) => boolean,
  changedMessage: string,
  unreadableMessage: string,
): Promise<void> {
  for (let attempt = 0; attempt <= SCHEDULER_SETTLE_DELAYS_MS.length; attempt += 1) {
    let beforeStartXml = "";
    try {
      beforeStartXml = readSchedulerXml();
    } catch {
      // Treat query errors like the default reader's empty result and retry below.
    }
    if (beforeStartXml.trim()) {
      if (!matchesExpected(beforeStartXml)) {
        throw new Error(changedMessage);
      }
      return;
    }
    const delayMs = SCHEDULER_SETTLE_DELAYS_MS[attempt];
    if (delayMs === undefined) break;
    await settle(delayMs);
  }
  throw new Error(unreadableMessage);
}

/**
 * Repair the already-installed background-service backend without switching managers.
 *
 * Windows scheduler: rewrite assets + stop/start; stale definitions are refreshed and may elevate.
 * Windows native: WinSW asset rewrite + restart (skips `install /p` when present).
 * macOS/Linux: re-run the user-level install/reload path.
 */
export async function repairService(deps: RepairServiceDeps = {}): Promise<void> {
  const diagnose = deps.diagnose ?? diagnoseService;
  const platform = deps.platform ?? process.platform;
  const diag = diagnose();
  if (!diag.supported) {
    throw new Error(`Background service is unsupported (${diag.summary}).`);
  }
  if (diag.conflict) {
    throw new Error(
      "Cannot repair while Task Scheduler and native WinSW are both present. "
        + "Run 'ocx service uninstall' then reinstall one backend with 'ocx service install'.",
    );
  }
  if (!diag.installed) {
    throw new Error("Background service is not installed. Run 'ocx service install' first.");
  }

  // Before anything is asserted, written, stopped or started. A repair that has already
  // rewritten the assets has changed the thing it was supposed to leave alone.
  const ownership = (deps.readOwnership ?? resolveServiceOwnership)();
  if (ownership.kind === "unknown") throw new Error(unknownServiceOwnerRefusal(ownership.reason));
  // `!== "cli"` rather than `=== "desktop"`: this is the CLI's own repair path, so any owner
  // that is not this CLI is one whose runtime it must not touch.
  if (ownership.kind === "owned" && ownership.ownership.owner !== "cli") {
    throw new Error(foreignServiceOwnerRefusal(ownership.ownership));
  }

  (deps.assertEnv ?? assertServiceEnvironmentMatchesInstall)();
  (deps.assertAuth ?? assertServiceAuthEnvironment)();
  const runtime = Object.freeze({ ...(deps.selectRuntime ?? durableBunRuntime)() });
  const configDir = (deps.configDir ?? getConfigDir)();
  (deps.assertRuntimeWritable ?? assertSelectedRuntimeWritable)(runtime, configDir, { platform, rootWasAbsent: !existsSync(configDir) });

  const temporaryLauncher = (deps.launcherPathDiagnostic ?? (() => serviceLauncherPathDiagnostic(undefined, platform, true)))();
  if (temporaryLauncher) console.warn(`⚠️ ${temporaryLauncher}`);

  if (platform === "win32") {
    if (diag.backend === "native") {
      await (deps.repairNative ?? ((selected: DurableBunRuntime) => installWinswService(defaultWinswEntry(serviceSourceDir, selected))))(runtime);
      (deps.writeNativeState ?? ((selected: DurableBunRuntime) => writeServiceInstallState("native", undefined, {}, selected)))(runtime);
      return;
    }
    const readSchedulerXml = deps.readSchedulerXml ?? statusWindowsXml;
    let registeredXml = "";
    try {
      registeredXml = readSchedulerXml();
    } catch {
      throw new Error(
        "Task Scheduler registration could not be read; repair stopped before changing or starting the service.",
      );
    }
    if (!registeredXml.trim()) {
      throw new Error(
        "Task Scheduler registration is empty or unreadable; repair stopped before changing or starting the service.",
      );
    }
    // Judge the definition against the same effective account the diagnostic uses. Relying on
    // the cached identity alone would make a scoped task this very version wrote look foreign
    // in a fresh process, and the message below would then name the wrong cause.
    const expectedUserId = (deps.resolveExpectedUserId ?? resolveWindowsTaskDiagnosticUserId)(registeredXml);
    const registrationHealthy = windowsTaskRegistrationHealthy(
      registeredXml,
      deps.schedulerWscript,
      deps.schedulerLauncher,
      expectedUserId,
    );
    const expectedValues = expectedUserId === null
      ? []
      : typeof expectedUserId === "string" ? [expectedUserId] : expectedUserId;
    const preferredSid = expectedValues[0];
    const triggers = taskXmlSection(taskXmlWithoutCommentsAndCdata(registeredXml), "Triggers");
    // An exact legacy account name is safe to recognize, but rewrite it to the
    // locale-independent SID while repair already owns the mutation boundary.
    const identityUpgradeNeeded = registrationHealthy
      && preferredSid !== undefined
      && !windowsTaskHasSessionRecoveryTriggers(triggers, preferredSid);
    // Omitted Priority also defaults to 7; background priority can starve health probes under CPU load.
    const priorityUpgradeNeeded = registrationHealthy && taskXmlOptionalValueEquals(
      taskXmlSection(taskXmlWithoutCommentsAndCdata(registeredXml), "Settings"), "Priority", "7",
    );
    const refreshableLegacy = windowsTaskRegistrationRefreshableLegacy(
      registeredXml,
      deps.schedulerWscript,
      deps.schedulerLauncher,
    );
    if (!registrationHealthy && !refreshableLegacy) {
      const scopedButUnresolved = expectedUserId === null
        && taskXmlElementCount(
          taskXmlSection(taskXmlWithoutCommentsAndCdata(registeredXml), "Triggers"),
          "UserId",
        ) > 0;
      throw new Error(
        scopedButUnresolved
          ? "The registered Task Scheduler triggers name an account, but the current Windows identity could not be resolved, so the registration could not be verified. "
            + "It was preserved and not replaced; re-run repair once the account can be resolved."
          : "Task Scheduler registration is not a recognized legacy OpenCodex definition; it was preserved for manual review.",
      );
    }
    try { (deps.stopScheduler ?? stopWindows)(); } catch { /* not running */ }
    (deps.writeSchedulerAssets ?? writeWindowsSchedulerAssets)(runtime);
    // Rewriting the on-disk assets does not touch the definition Task Scheduler holds, so a
    // task registered by an older version keeps its old triggers forever: status reports it
    // stale, tells the user to run repair, and repair changes nothing it complains about.
    // Re-register only when the registered XML is actually stale, so the ordinary repair
    // stays free of `schtasks /create` and its UAC prompt.
    let startExpectedXml = registeredXml;
    if (!registrationHealthy || identityUpgradeNeeded || priorityUpgradeNeeded) {
      // The task was stopped above, so a failed replacement must not exit here: `/create /f`
      // can be rejected, elevation can be cancelled, and staging or verification can fail.
      // Any of those would leave a previously runnable proxy stopped and the user worse off
      // than before the repair. Restart the definition still registered and surface the
      // original failure instead.
      const attemptNonce = randomUUID();
      try {
        await (deps.reregisterScheduler ?? reregisterWindowsSchedulerTask)(attemptNonce, registeredXml);
        let replacementXml = "";
        try {
          replacementXml = readSchedulerXml();
        } catch {
          throw new Error("The refreshed Task Scheduler registration could not be read back.");
        }
        if (
          !windowsTaskRegistrationHealthy(replacementXml)
          || !windowsTaskRegistrationOwnedByAttempt(replacementXml, attemptNonce)
        ) {
          throw new Error(
            "The refreshed Task Scheduler registration failed live shape or attempt-ownership verification.",
          );
        }
        startExpectedXml = replacementXml;
      } catch (err) {
        const recoveryErrors: unknown[] = [];
        let restartExpectedXml: string | null = null;
        let currentXml: string | null = null;
        try {
          currentXml = readSchedulerXml();
        } catch {
          recoveryErrors.push(new Error(
            "Task Scheduler state became unreadable after the failed replacement; it was preserved and not started.",
          ));
        }

        if (currentXml !== null) {
          if (windowsSchedulerRegistrationMatchesSnapshot(currentXml, registeredXml)) {
            restartExpectedXml = registeredXml;
          } else if (currentXml.trim()) {
            const attemptOwned = windowsTaskRegistrationOwnedByAttempt(currentXml, attemptNonce);
            if (attemptOwned && windowsTaskRegistrationHealthy(currentXml)) {
              restartExpectedXml = currentXml;
            } else {
              recoveryErrors.push(new Error(
                attemptOwned
                  ? "The failed repair left an unhealthy attempt-owned registration; it was preserved and not started."
                  : windowsTaskRegistrationHealthy(currentXml)
                    ? "A different healthy OpenCodex Task Scheduler registration appeared during repair; it was preserved and not started."
                    : "A different or unhealthy Task Scheduler registration appeared during repair; it was preserved and not started.",
              ));
            }
          } else {
            let probe: WindowsSchedulerTaskProbe;
            try {
              probe = (deps.probeScheduler ?? (() => probeWindowsSchedulerTask(TASK)))();
            } catch {
              probe = { status: "unknown", detail: "presence probe failed" };
            }
            if (probe.status === "absent") {
              try {
                await (deps.restoreSchedulerIfAbsent ?? restoreWindowsSchedulerTaskIfAbsent)(registeredXml);
                restartExpectedXml = registeredXml;
              } catch (error) {
                recoveryErrors.push(error);
              }
            } else {
              recoveryErrors.push(new Error(probe.status === "present"
                ? "A Task Scheduler registration is present but its XML is unreadable; it was preserved and not started."
                : `Task Scheduler state is unknown after the failed replacement (${probe.detail}); no registration was overwritten or started.`));
            }
          }
        }

        if (restartExpectedXml !== null) {
          try {
            await assertSchedulerSnapshotBeforeStart(
              readSchedulerXml,
              restartExpectedXml,
              deps.settleSchedulerRead ?? settleDelay,
              "The Task Scheduler registration changed again before restart; the newer definition was preserved and not started.",
              "Task Scheduler state remained unreadable before restart; the registration was preserved and not started.",
            );
            (deps.startScheduler ?? startWindows)();
          } catch (error) {
            recoveryErrors.push(error);
          }
        }
        if (recoveryErrors.length > 0) {
          throw new AggregateError(
            [err, ...recoveryErrors],
            "Task Scheduler repair failed; concurrent or unverified scheduler state was preserved.",
          );
        }
        throw err;
      }
    }
    // The final live read is the proof that `/run` still targets the definition this repair
    // verified. A failed `schtasks /query` becomes an empty string, so allow only a bounded
    // retry for that unreadable state. A readable mismatch is authoritative and fails
    // immediately; presence alone cannot prove that the fixed-name task still has our XML.
    await assertSchedulerSnapshotBeforeStart(
      readSchedulerXml,
      startExpectedXml,
      deps.settleSchedulerRead ?? settleDelay,
      "Task Scheduler registration changed before restart; the current definition was preserved and not started.",
      "Task Scheduler registration became unreadable before restart; it was preserved and not started.",
    );
    (deps.startScheduler ?? startWindows)();
    (deps.writeSchedulerState ?? ((selected: DurableBunRuntime) => writeServiceInstallState("scheduler", undefined, {}, selected)))(runtime);
    return;
  }


  throw new Error(`Background service repair is unsupported on ${platform}.`);
}
