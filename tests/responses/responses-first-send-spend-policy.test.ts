import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { closeRequestHistoryIndex } from "../../backend/src/routing/history/indexer";
import { clearResponseStateForTests, flushResponseState } from "../../backend/src/responses/state";
import { clearKeyCooldowns } from "../../backend/src/providers/key-failover";
import { resetProviderRequestPacingForTest } from "../../backend/src/providers/request-pacing";
import { createPhysicalSendReporter, createRequestExecutionBudget } from "../../backend/src/lib/request-execution-budget";
import { createSpendReservationLedger, DEFAULT_SPEND_RESERVATION_POLICY, type SpendJournal, type SpendReservationPolicy } from "../../backend/src/lib/spend-reservation-ledger";
import { SendBudgetExhaustedError, providerErrorDestinationLabel, fetchWithTransientRetry } from "../../backend/src/lib/upstream-retry";
import { createTranslatorBudget } from "../../backend/src/lib/translator-budget";
import { createRequestSpendTracker } from "../../backend/src/server/responses/request-spend";
import { createResponsesSendBudget } from "../../backend/src/server/responses/request-send-budget";
import { handleResponses } from "../../backend/src/server/responses/core";
import type { RequestLogContext } from "../../backend/src/server/request-log";
import type { OcxConfig } from "../../backend/src/types";
import { acquireOwnedSpendHome } from "../helpers/owned-spend-home";
import { installIsolatedCodexHome, type IsolatedCodexHome } from "../helpers/isolated-codex-home";
import { removeTreeWithRetry } from "../helpers/remove-tree";
import { responsesSuccess } from "../helpers/combo-failover-upstream";

/**
 * A Combo child decides once per request whether its prepaid first send is settled by the
 * physical-dispatch receipt (spend enforcement inactive) or by the shared physical-send
 * reporter (enforcement active). These pin what that decision relies on.
 */
let home: string, prior: string | undefined, codex: IsolatedCodexHome, release: () => void;
const servers: Array<ReturnType<typeof Bun.serve>> = [];
beforeEach(() => {
  prior = process.env.OPENCODEX_HOME;
  home = mkdtempSync(join(tmpdir(), "first-send-spend-policy-"));
  process.env.OPENCODEX_HOME = home;
  codex = installIsolatedCodexHome("first-send-spend-policy-codex-");
  release = acquireOwnedSpendHome(); clearKeyCooldowns();
});
afterEach(async () => {
  release();
  for (const server of servers.splice(0)) await server.stop(true);
  closeRequestHistoryIndex(); await flushResponseState(); clearResponseStateForTests(); clearKeyCooldowns();
  resetProviderRequestPacingForTest();
  if (prior === undefined) delete process.env.OPENCODEX_HOME;
  else process.env.OPENCODEX_HOME = prior;
  codex.restore(); removeTreeWithRetry(home);
});

const memoryJournal = (): SpendJournal => {
  const lines: string[] = [];
  return { read: () => [...lines], append: line => { lines.push(line); }, rewrite: next => { lines.splice(0, lines.length, ...next); } };
};
const policyWith = (poolMaxTokens: number | undefined): SpendReservationPolicy => ({
  ...DEFAULT_SPEND_RESERVATION_POLICY, pool: poolMaxTokens === undefined ? {} : { maxTokens: poolMaxTokens },
});
/** A real tracker over an injected ledger, so enforcement follows the ledger's live policy. */
function trackerFixture(enforced: boolean) {
  const ledger = createSpendReservationLedger({ journal: memoryJournal(), salt: "first-send-policy", policy: policyWith(enforced ? 1_000_000 : undefined) });
  const log: RequestLogContext = { model: "m", provider: "a", spendPoolId: "a", usageLogInputTokens: 10, spendOutputCeilingTokens: 0 };
  const tracker = createRequestSpendTracker(log, undefined, ledger);
  const budget = createRequestExecutionBudget(undefined, undefined, tracker);
  return { ledger, log, tracker, budget };
}

for (const enforced of [false, true])

test("enforced spend counts a local refusal after beforeSend as one started send", async () => {
  // Same accounting #6763's native Chat path has: `beforeSend` claims the send, and the retry
  // helper reports it in `finally` whatever the send did, including a local refusal.
  const f = trackerFixture(true);
  const booking = f.budget.reserveDispatch({ sendClass: "initial", targetKey: "a/m", countedExternally: true });
  if (!booking.allowed) throw new Error("fixture reservation refused");
  const report = createPhysicalSendReporter(f.budget, () => ({ poolId: "a" }), booking.permit);
  await expect(fetchWithTransientRetry(async () => { throw new SendBudgetExhaustedError(providerErrorDestinationLabel("fixture")); },
    { attempts: 1, onSendsConsumed: report })).rejects.toBeInstanceOf(SendBudgetExhaustedError);
  expect(f.budget.physicalStarted).toBe(1);
  expect(f.budget.used).toBe(1);
  booking.permit.release(); // The booking was consumed by the started send: nothing to refund.
  expect(f.budget.used).toBe(1);
});
