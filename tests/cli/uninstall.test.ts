import { afterEach, describe, expect, test } from "bun:test";
import {
  setUninstallServiceHooksForTests,
  uninstallServiceIfInstalled,
} from "../../src/service";
import { pathToFileURL } from "node:url";
import { repoRoot } from "../helpers/repo-root";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { removeOwnedConfigAfterDesktopCleanup, type UninstallClientStateDeps } from "../../src/cli/uninstall-client-state";
import { cleanupOwnedIntegrationsBeforeUninstall } from "../../src/cli/uninstall-integrations";
import { assertClientLifecycleHeld, withClientLifecycle, withClientLifecycleSync, type ClientLifecycleHeld } from "../../src/client/lifecycle-lock";
import type { UninstallObservation } from "../../src/cli/uninstall-plan";
import type { ExportModel } from "../../src/clients/config-export";
import { INTEGRATION_CLIENTS } from "../../src/integrations/registry";
import { createIntegrationStateStore } from "../../src/integrations/store";
import { applyIntegration, disableIntegrationCoordinated } from "../../src/integrations/writer";
import type { OcxConfig } from "../../src/types";

const root = pathToFileURL(repoRoot() + "/");

async function readText(path: string): Promise<string> {
  return await Bun.file(new URL(path, root)).text();
}

describe("full uninstall command", () => {
  afterEach(() => setUninstallServiceHooksForTests(null));

  test("CLI exposes a one-shot local state cleanup command", async () => {
    const dispatch = await readText("src/cli/dispatch.ts");

    expect(dispatch).toContain("uninstall: async");
    const cli = await readText("src/cli/index.ts");
    expect(cli).toContain("async function handleUninstall()");
    expect(cli).toContain("uninstallServiceIfInstalled");
    expect(cli).toContain("uninstallCodexShim");
    expect(cli).toContain("restoreNativeCodex");
    expect(cli).toContain("await removeOwnedConfigAfterDesktopCleanup(observed)");
    expect(cli).not.toContain("removeOwnedConfigState(getConfigDir())");
    expect(cli).not.toContain("rmSync(getConfigDir()");
  });

  test("an elevated-run refusal exits before the first teardown step", async () => {
    const cli = await readText("src/cli/index.ts");
    const body = cli.slice(cli.indexOf("async function handleUninstall()"));
    const refusal = body.indexOf("const privilegeRefusal = uninstallPrivilegeRefusal(getConfigDir());");
    const exit = body.indexOf("if (privilegeRefusal) { console.error(`❌ ${privilegeRefusal}`); process.exit(1); }");
    expect(refusal).toBeGreaterThan(0);
    expect(exit).toBeGreaterThan(refusal);
    // Nothing before the check: every step that stops, removes or rewrites anything comes after the exit.
    expect(body.slice(0, refusal).split("\n").filter(line => line.trim() && !line.trim().startsWith("//"))).toEqual(["async function handleUninstall() {"]);
    for (const step of ["await runStep(", "stopServiceIfInstalledDetailed", "uninstallServiceDetailed", "restoreNativeCodexAsync", "revertSystemEnv", "uninstallCodexShimAsync", "removeOwnedConfigAfterDesktopCleanup"]) {
      expect(body.indexOf(step)).toBeGreaterThan(exit);
    }
  });

  test("CLI exposes explicit legacy history recovery command", async () => {
    const dispatch = await readText("src/cli/dispatch.ts");
    const cli = await readText("src/cli/index.ts");

    expect(dispatch).toContain('"recover-history": async');
    expect(cli).toContain("ocx recover-history --legacy-openai");
    expect(cli).toContain("async function handleRecoverHistory()");
    // The command still performs legacy recovery, but through the serialized
    // history job rather than by calling the writer inline — the operation name
    // is what keeps it distinct from a generic restore, which must not touch the
    // backup manifest this one deliberately leaves alone.
    expect(cli).toContain("recover-legacy-openai");
    expect(cli).toContain("runCodexHistoryJob");
  });

  test("service cleanup has a quiet best-effort helper", async () => {
    const service = await readText("src/service/orchestration.ts");

    expect(service).toContain("export function uninstallServiceIfInstalled()");
    expect(service).toContain("uninstallLaunchd");
    expect(service).toContain("uninstallWindows");
    expect(service).toContain("uninstallSystemd");
  });

  test("native service removal failure propagates without deleting install state", () => {
    const calls: string[] = [];
    let stateRemovals = 0;
    setUninstallServiceHooksForTests({
      platform: "win32",
      assertEnvironment: () => {},
      probeWindowsTask: () => ({ status: "present" }),
      uninstallWindowsTask: () => { calls.push("scheduler"); },
      nativeStatus: () => "started",
      uninstallNative: () => {
        calls.push("native");
        throw new Error("native removal failed");
      },
      removeInstallState: () => { stateRemovals++; },
    });

    expect(() => uninstallServiceIfInstalled()).toThrow("native removal failed");
    expect(calls).toEqual(["scheduler", "native"]);
    expect(stateRemovals).toBe(0);
  });

  test("scheduler removal failure propagates without deleting install state", () => {
    let stateRemovals = 0;
    setUninstallServiceHooksForTests({
      platform: "win32",
      assertEnvironment: () => {},
      probeWindowsTask: () => ({ status: "present" }),
      uninstallWindowsTask: () => { throw new Error("scheduler removal failed"); },
      nativeStatus: () => "nonexistent",
      uninstallNative: () => {},
      removeInstallState: () => { stateRemovals++; },
    });

    expect(() => uninstallServiceIfInstalled()).toThrow("scheduler removal failed");
    expect(stateRemovals).toBe(0);
  });

  test("full uninstall kills the tracked proxy before deleting service assets", async () => {
    const cli = await readText("src/cli/index.ts");
    const uninstallBody = cli.slice(cli.indexOf("async function handleUninstall()"), cli.indexOf("type HealthCheck"));

    expect(uninstallBody).toContain('runStep("service stopped"');
    expect(uninstallBody).toContain('runStep("proxy stopped"');
    expect(uninstallBody).toContain('runStep("service removed"');
    expect(uninstallBody).toContain("await stopProxy(pid);");
    expect(uninstallBody).toContain("uninstallServiceDetailed()");
    expect(uninstallBody.indexOf('runStep("service stopped"')).toBeLessThan(uninstallBody.indexOf('runStep("proxy stopped"'));
    expect(uninstallBody.indexOf('runStep("proxy stopped"')).toBeLessThan(uninstallBody.indexOf('runStep("service removed"'));
    expect(uninstallBody.indexOf("await stopProxy(pid);")).toBeLessThan(uninstallBody.indexOf("uninstallServiceDetailed()"));
  });

  test("restore forwards the explicit provider-table removal flag and warns before mutation", async () => {
    const dispatch = await readText("src/cli/dispatch.ts");
    const restoreStart = dispatch.indexOf("restore: async deps => {");
    const restoreBody = dispatch.slice(restoreStart, dispatch.indexOf('"recover-history": async', restoreStart));

    expect(restoreBody).toContain('takeFlag(restoreArgs, "--remove-codex-provider-table")');
    expect(restoreBody).toContain("conversations already tagged opencodex will stop opening");
    expect(restoreBody).toContain("restoreNativeCodexAsync({ revalidateDesiredState: true, removeProviderTable })");
  });
});
describe("uninstall gates shared teardown on a proven service stop", () => {
  test("the authorization rule, exercised for every failure permutation", async () => {
    const { sharedTeardownAuthorized } = await import("../../src/cli/uninstall-plan");
    const base = {
      serviceStop: "stopped" as const,
      proxyProvenDown: true,
      serviceRemoval: "removed" as const,
      respawnWindowVerified: false,
    };
    expect(sharedTeardownAuthorized(base)).toBe(true);
    expect(sharedTeardownAuthorized({ ...base, serviceStop: "absent" })).toBe(true);
    expect(sharedTeardownAuthorized({ ...base, serviceRemoval: "absent" })).toBe(true);
    // Removing the registration does not prove an already-running wrapper died; killing it
    // is best-effort (#764), so the restart window has to be polled first.
    expect(sharedTeardownAuthorized({ ...base, serviceStop: "stopped-respawnable" })).toBe(false);
    expect(sharedTeardownAuthorized({ ...base, serviceStop: "stopped-respawnable", respawnWindowVerified: true })).toBe(true);
    // A manager that refused to stop, or one we could not read, may still be running.
    expect(sharedTeardownAuthorized({ ...base, serviceStop: "failed" })).toBe(false);
    expect(sharedTeardownAuthorized({ ...base, serviceStop: "state-unknown" })).toBe(false);
    // The step itself threw: we know nothing.
    expect(sharedTeardownAuthorized({ ...base, serviceStop: null })).toBe(false);
    // A proxy that could not be PROVEN down — a live orphan with no pid, or an endpoint
    // that would not answer — blocks it. A findLiveProxy miss is not proof.
    expect(sharedTeardownAuthorized({ ...base, proxyProvenDown: false })).toBe(false);
    // A removal that failed used to look like absence on darwin and linux.
    expect(sharedTeardownAuthorized({ ...base, serviceRemoval: "failed" })).toBe(false);
    expect(sharedTeardownAuthorized({ ...base, serviceRemoval: null })).toBe(false);
  });

  test("a removal failure is distinguishable from nothing being installed", async () => {
    const { setUninstallServiceHooksForTests, uninstallServiceDetailed } = await import("../../src/service");
    // Windows is the platform whose hooks are injectable; the darwin/linux catch arms that
    // returned the same false as absence are now typed outcomes rather than a boolean.
    setUninstallServiceHooksForTests({
      platform: "win32",
      assertEnvironment: () => {},
      probeWindowsTask: () => ({ status: "absent" }) as never,
      uninstallWindowsTask: () => {},
      nativeStatus: () => "nonexistent",
      uninstallNative: () => {},
      removeInstallState: () => {},
    } as never);
    expect(uninstallServiceDetailed()).toBe("absent");

    const serviceSource = await readText("src/service/orchestration.ts");
    // The darwin and linux arms return "failed", not the absence value.
    expect(serviceSource).toContain('try { uninstallLaunchd(); removeServiceInstallState(); return "removed"; } catch { return "failed"; }');
    expect(serviceSource).toContain('try { unlinkSync(unitPath()); removeServiceInstallState(); return "removed"; } catch { return "failed"; }');
  });

  test("a live orphan with no pid file blocks the teardown", async () => {
    const cli = await readText("src/cli/index.ts");
    const at = cli.indexOf("async function handleUninstall(");
    const fn = cli.slice(at, at + 9000);
    // A missing pid file is not proof that nothing is serving — the same discovery
    // `ocx stop` performs. Without it, uninstall restored shared config under a live proxy.
    expect(fn).toContain("const live = await findLiveProxy();");
    expect(fn).toContain("observed.proxyProvenDown = await proxyEndpointProvenDown();");
    expect(fn).toContain("no process id could be resolved for it");
    // The orphan-with-no-pid branch THROWS, so `proxyProvenDown` stays false and the
    // authorization rule refuses the shared teardown.
    const orphanBranch = fn.slice(fn.indexOf("const live = await findLiveProxy();"), fn.indexOf("const live = await findLiveProxy();") + 600);
    expect(orphanBranch).toContain("throw new Error(");
    // A findLiveProxy miss is not proof either: it goes through the tri-state probe first.
    expect(orphanBranch).toContain("could not be confirmed down either");
  });

  async function uninstallFn(): Promise<string> {
    const cli = await readText("src/cli/index.ts");
    const at = cli.indexOf("async function handleUninstall(");
    expect(at).toBeGreaterThan(-1);
    return cli.slice(at, at + 9000);
  }

  test("the detailed outcome is consumed, not the boolean collapse", async () => {
    const fn = await uninstallFn();
    // stopServiceIfInstalled returns false for "not installed", "refused to stop" and
    // "state could not be read" alike, so this step reported "not installed" for a manager
    // that might still be running (#3008).
    expect(fn).toContain("stopServiceIfInstalledDetailed()");
    expect(fn).not.toContain("stopServiceIfInstalled()");
    expect(fn).toContain('if (outcome === "absent") return false;');
    expect(fn).toContain('if (outcome === "failed")');
    expect(fn).toContain('if (outcome === "state-unknown")');
  });

  test("shared teardown runs only when nothing that could still serve is unaccounted for", async () => {
    const fn = await uninstallFn();
    // The rule itself is exercised by calling it above; this pins the wiring.
    expect(fn).toContain("if (sharedTeardownAuthorized(observed)) {");
    // Every step that could leave something serving records what it observed, and the
    // fields start pessimistic so a step that throws cannot look like a success.
    expect(fn).toContain("serviceStop: null,");
    expect(fn).toContain("proxyProvenDown: false,");
    expect(fn).toContain("serviceRemoval: null,");
    expect(fn).toContain("respawnWindowVerified: false,");
    expect(fn).toContain("observed.serviceStop = outcome;");
    expect(fn).toContain("observed.serviceRemoval = outcome;");
    expect(fn).toContain('if (observed.serviceStop === "stopped-respawnable")');
    expect(fn).toContain("observed.respawnWindowVerified = true;");
    const gateAt = fn.indexOf("if (sharedTeardownAuthorized(observed)) {");
    expect(gateAt).toBeLessThan(fn.indexOf("native Codex restored", gateAt));
    const nativeRestoreStep = fn.slice(
      fn.indexOf('runStep("native Codex restored"', gateAt),
      fn.indexOf('runStep("Grok Build config restored"', gateAt),
    );
    // A partial config artifact with success=true discharged routing. Uninstall must report
    // the retained table and continue, rather than adding this step to the failure list.
    expect(nativeRestoreStep).toContain("if (!r.success) throw new Error(r.message);");
    expect(nativeRestoreStep).toContain("if (r.retainedCodexProviderTable)");
    expect(nativeRestoreStep).not.toContain('state === "partial"');
    // The skip is a failure, not a silent pass: the command must exit nonzero and say what
    // to run once the blocker is resolved.
    expect(fn).toContain('failures.push("native Codex restored", "Grok Build config restored");');
    expect(fn).toContain("Skipping shared teardown");
    // Naming only `ocx restore` was wrong: it restores client routing but leaves the
    // service removal and local cleanup this command had not reached.
    expect(fn).toContain("rerun 'ocx uninstall'");
    expect(fn).toContain("interim step");
  });
});
  test("proof covers every distinct endpoint, not just the preferred one", async () => {
    const { endpointsToProve, everyEndpointProvenDown, everyEndpointProvenDownAsync } = await import("../../src/cli/uninstall-plan");

    // A stale runtime record pointing at a closed port, and the live proxy on the
    // configured one. Probing only the runtime candidate reports "dead" for a port nobody
    // is using and authorizes the teardown (#3008).
    const endpoints = endpointsToProve({ port: 10999, hostname: "127.0.0.1" }, { port: 10100, hostname: "127.0.0.1" });
    expect(endpoints).toEqual([
      { hostname: "127.0.0.1", port: 10999 },
      { hostname: "127.0.0.1", port: 10100 },
    ]);
    const closedRuntimeLiveConfig = (e: { port: number }) => (e.port === 10999 ? "dead" as const : "live" as const);
    expect(everyEndpointProvenDown(endpoints, closedRuntimeLiveConfig)).toBe(false);
    // A silent listener is not absence either.
    expect(everyEndpointProvenDown(endpoints, e => (e.port === 10999 ? "dead" : "unknown"))).toBe(false);
    // Both definitively dead is the only proof.
    expect(everyEndpointProvenDown(endpoints, () => "dead")).toBe(true);

    // Identical candidates collapse to one; a missing runtime record leaves the config one.
    expect(endpointsToProve({ port: 10100, hostname: "127.0.0.1" }, { port: 10100 })).toHaveLength(1);
    expect(endpointsToProve(null, { port: 10100 })).toEqual([{ hostname: "127.0.0.1", port: 10100 }]);
    // No configured port still yields the default, so the set is never empty in practice.
    expect(endpointsToProve(null, {})).toEqual([{ hostname: "127.0.0.1", port: 10100 }]);
    // An empty set is not proof of anything.
    expect(everyEndpointProvenDown([], () => "dead")).toBe(false);
    expect(await everyEndpointProvenDownAsync(endpoints, async () => "dead")).toBe(true);
    expect(await everyEndpointProvenDownAsync([], async () => "dead")).toBe(false);
    // A nonsense runtime port is skipped rather than probed.
    expect(endpointsToProve({ port: 0 }, { port: 10100 })).toEqual([{ hostname: "127.0.0.1", port: 10100 }]);
  });

  test("the respawn window is verified by evidence, not by a silent poll", async () => {
    const cli = await readText("src/cli/index.ts");
    const at = cli.indexOf("async function handleUninstall(");
    const fn = cli.slice(at, at + 9000);
    // proxyStillLiveAfterStop returns null on a timeout as well as on a genuinely dead
    // endpoint, so a respawned-but-unresponsive proxy looked verified-down.
    const windowStep = fn.slice(fn.indexOf('runStep("respawn window verified"'), fn.indexOf('runStep("respawn window verified"') + 900);
    expect(windowStep).toContain("if (!await proxyEndpointProvenDown())");
    expect(windowStep).toContain("could not be confirmed down either");
    expect(windowStep.indexOf("if (!await proxyEndpointProvenDown())"))
      .toBeLessThan(windowStep.indexOf("observed.respawnWindowVerified = true;"));
    // And the proof itself asks every candidate.
    expect(fn).toContain("endpointsToProve(readRuntimePort(), loadConfig())");
    expect(fn).toContain("everyEndpointProvenDownAsync(endpoints, probeEndpointLiveness)");
  });

const safeTeardown: UninstallObservation = {
  serviceStop: "stopped", serviceRemoval: "removed", proxyProvenDown: true, respawnWindowVerified: false,
};
describe("uninstall restores recorded third-party integrations before deleting recovery state", () => {
  const models: ExportModel[] = [
    { namespaced: "fixture/model", provider: "fixture", id: "model", contextWindow: 128_000 },
  ];
  const config = {
    port: 10100,
    hostname: "127.0.0.1",
    defaultProvider: "fixture",
    providers: { fixture: { adapter: "openai-chat", baseUrl: "http://127.0.0.1/v1" } },
  } as unknown as OcxConfig;

  async function fixture() {
    const root = mkdtempSync(join(tmpdir(), "ocx-uninstall-integrations-"));
    const home = join(root, "home");
    const configDir = join(root, "opencodex");
    const lockPath = join(root, "runtime", "lifecycle.sqlite");
    const env = {} as NodeJS.ProcessEnv;
    mkdirSync(INTEGRATION_CLIENTS.pi.detectDir(env, home), { recursive: true });
    mkdirSync(configDir, { recursive: true });
    const clientConfig = INTEGRATION_CLIENTS.pi.configPath(env, home);
    mkdirSync(dirname(clientConfig), { recursive: true });
    writeFileSync(clientConfig, '{"userSetting":"keep"}\n');
    const store = createIntegrationStateStore(join(configDir, "integrations"));
    const input = { clientId: "pi" as const, models, config, port: config.port, env, home, store };
    expect(applyIntegration(input).ok).toBe(true);
    const deps: UninstallClientStateDeps = {
      withLifecycle: work => withClientLifecycle(work, { lockPath }),
      cleanupIntegrations: () => cleanupOwnedIntegrationsBeforeUninstall({
        createStore: () => store,
        loadConfig: () => config,
        loadModels: async () => models,
        disable: value => disableIntegrationCoordinated(value),
        env,
        home,
      }),
      remove: () => {
        rmSync(configDir, { recursive: true });
        return { status: "removed", residualPaths: [] };
      },
      aclReapPending: () => false,
      removePickerArtifacts: async () => ({ ok: true }),
    };
    return { root, home, env, configDir, clientConfig, store, deps };
  }

  function addAsideProfiles(f: Awaited<ReturnType<typeof fixture>>) {
    const asideRoot = join(f.home, ".aside");
    const profiles = [0, 1].map(id => {
      const detectDir = join(asideRoot, "u", String(id));
      const configPath = join(detectDir, "models.json");
      mkdirSync(detectDir, { recursive: true });
      writeFileSync(configPath, '{"userSetting":"keep"}\n');
      const store = id === 0 ? f.store
        : createIntegrationStateStore(join(f.store.root, "aside-profiles", String(id)));
      return { id, detectDir, configPath, store };
    });
    // The legacy owner is not the current profile; uninstall must use its recorded path.
    writeFileSync(join(asideRoot, "accounts.json"), JSON.stringify({
      currentAccountId: 1, accounts: profiles.map(({ id }) => ({ id })),
    }));
    for (const profile of profiles) {
      expect(applyIntegration({ clientId: "aside", models, config, port: config.port,
        env: f.env, home: f.home, store: profile.store,
        resolvedPaths: { configPath: profile.configPath, detectDir: profile.detectDir },
      }).ok).toBe(true);
    }
    return profiles;
  }

  test("restores the external file before removing the records that authorize restoration", async () => {
    const f = await fixture();
    try {
      expect(await removeOwnedConfigAfterDesktopCleanup(safeTeardown, f.deps))
        .toEqual({ status: "removed", residualPaths: [] });
      expect(existsSync(f.configDir)).toBe(false);
      const restored = JSON.parse(readFileSync(f.clientConfig, "utf8")) as Record<string, unknown>;
      expect(restored.userSetting).toBe("keep");
      expect((restored.providers as Record<string, unknown> | undefined)?.opencodex).toBeUndefined();
    } finally {
      rmSync(f.root, { recursive: true, force: true });
    }
  });

  test("a conflicting external edit retains both the file and recovery records", async () => {
    const f = await fixture();
    try {
      const edited = JSON.parse(readFileSync(f.clientConfig, "utf8")) as {
        providers: Record<string, Record<string, unknown>>;
      };
      edited.providers.opencodex!.baseUrl = "http://user-edited.invalid/v1";
      writeFileSync(f.clientConfig, `${JSON.stringify(edited, null, 2)}\n`);
      await expect(removeOwnedConfigAfterDesktopCleanup(safeTeardown, f.deps))
        .rejects.toThrow("integration cleanup refused for pi");
      expect(existsSync(f.configDir)).toBe(true);
      expect(f.store.readRecordsStrict().pi).toBeDefined();
      expect(readFileSync(f.clientConfig, "utf8")).toContain("user-edited.invalid");
    } finally {
      rmSync(f.root, { recursive: true, force: true });
    }
  });

  test("restores legacy and child Aside profiles before deleting all recovery stores", async () => {
    const f = await fixture();
    try {
      const profiles = addAsideProfiles(f);
      expect(await removeOwnedConfigAfterDesktopCleanup(safeTeardown, f.deps))
        .toEqual({ status: "removed", residualPaths: [] });
      expect(existsSync(f.configDir)).toBe(false);
      for (const profile of profiles) {
        expect(JSON.parse(readFileSync(profile.configPath, "utf8")))
          .toEqual({ userSetting: "keep" });
      }
    } finally { rmSync(f.root, { recursive: true, force: true }); }
  });

  test("an unreadable Aside child record prevents every disable and config removal", async () => {
    const f = await fixture();
    try {
      const profiles = addAsideProfiles(f);
      const originals = profiles.map(profile => readFileSync(profile.configPath, "utf8"));
      const childRecords = join(profiles[1]!.store.root, "records.json");
      writeFileSync(childRecords, "invalid JSON");
      await expect(removeOwnedConfigAfterDesktopCleanup(safeTeardown, f.deps))
        .rejects.toThrow("integration ownership is invalid for recovery");
      expect(existsSync(f.configDir)).toBe(true);
      expect(f.store.readRecordsStrict().pi).toBeDefined();
      expect(profiles.map(profile => readFileSync(profile.configPath, "utf8"))).toEqual(originals);
      expect(readFileSync(childRecords, "utf8")).toBe("invalid JSON");
    } finally { rmSync(f.root, { recursive: true, force: true }); }
  });

  test("a conflicted Aside child retains recovery state without undoing earlier disables", async () => {
    const f = await fixture();
    try {
      const profiles = addAsideProfiles(f);
      const child = profiles[1]!;
      const edited = JSON.parse(readFileSync(child.configPath, "utf8"));
      edited.providers.opencodex.baseUrl = "http://user-edited.invalid/v1";
      writeFileSync(child.configPath, `${JSON.stringify(edited)}\n`);
      await expect(removeOwnedConfigAfterDesktopCleanup(safeTeardown, f.deps))
        .rejects.toThrow("integration cleanup refused for aside");
      expect(existsSync(f.configDir)).toBe(true);
      expect(child.store.readRecordsStrict().aside).toBeDefined();
      expect(readFileSync(child.configPath, "utf8")).toContain("user-edited.invalid");
      expect(f.store.readRecordsStrict().pi).toBeUndefined();
      expect(f.store.readRecordsStrict().aside).toBeUndefined();
      expect(JSON.parse(readFileSync(profiles[0]!.configPath, "utf8"))).toEqual({ userSetting: "keep" });
    } finally { rmSync(f.root, { recursive: true, force: true }); }
  });

  test("an unregistered owned Aside profile refuses cleanup rather than dropping its proof", async () => {
    const f = await fixture();
    try {
      const profiles = addAsideProfiles(f);
      writeFileSync(join(f.home, ".aside", "accounts.json"), JSON.stringify({
        currentAccountId: 0, accounts: [{ id: 0 }],
      }));
      await expect(removeOwnedConfigAfterDesktopCleanup(safeTeardown, f.deps))
        .rejects.toThrow("Aside profile ownership is missing or mismatched");
      expect(existsSync(f.configDir)).toBe(true);
      expect(f.store.readRecordsStrict().pi).toBeDefined();
      expect(profiles[1]!.store.readRecordsStrict().aside).toBeDefined();
    } finally { rmSync(f.root, { recursive: true, force: true }); }
  });
});
