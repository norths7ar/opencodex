import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { handleAccountPolicyCommand } from "../../backend/src/cli/account-policy";
import { createTempHome, type TempHome } from "../helpers/temp-home";
let home: TempHome;
let out: ReturnType<typeof spyOn>, err: ReturnType<typeof spyOn>;
beforeEach(() => { home = createTempHome("ocx-cli-anthropic2-"); out = spyOn(console, "log").mockImplementation(() => {}); err = spyOn(console, "error").mockImplementation(() => {}); });
afterEach(() => { out.mockRestore(); err.mockRestore(); home.remove(); });
const grants = { accountId: "same", eligible: false, ineligibleReason: "no_grant", atLimit: false, grants: [], nextGrantId: null, weeklyResetsAt: null, cooldownUntil: null, pendingOperation: null, journalAvailable: true };
test("reset status explicitly targets B and rejects A or missing identity", async () => {
  for (const provider of ["anthropic2", "anthropic", undefined]) {
    const calls: { url: URL; method: string }[] = [];
    const deps = { baseUrl: "http://127.0.0.1:10100", fetchImpl: (async (input, init) => { calls.push({ url: new URL(String(input)), method: init?.method ?? "GET" }); return Response.json({ ...grants, provider, secret: "PRIVATE" }); }) as typeof fetch };
    const code = await handleAccountPolicyCommand("anthropic-reset-grants", ["same", "--provider", "anthropic2", "--json"], deps);
    expect(code).toBe(provider === "anthropic2" ? 0 : 1);
    expect(calls).toHaveLength(1); expect(calls[0]!.method).toBe("GET");
    expect(calls[0]!.url.searchParams.get("provider")).toBe("anthropic2");
    expect(calls[0]!.url.searchParams.get("accountId")).toBe("same");
    expect(JSON.stringify(out.mock.calls)).not.toContain("PRIVATE"); out.mockClear();
  }
});
test("legacy reset remains A without a provider query; invalid providers make no request", async () => {
  const calls: string[] = [];
  const deps = { baseUrl: "http://127.0.0.1:10100", fetchImpl: (async input => { calls.push(String(input)); return Response.json(grants); }) as typeof fetch };
  expect(await handleAccountPolicyCommand("anthropic-reset-grants", ["--json"], deps)).toBe(0);
  expect(new URL(calls[0]!).search).toBe("");
  expect(await handleAccountPolicyCommand("anthropic-reset-grants", ["--provider", "anthropic3"], deps)).toBe(2);
  expect(calls).toHaveLength(1);
});
