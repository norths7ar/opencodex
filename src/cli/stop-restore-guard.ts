import { withConfigMutationLockSync } from "../config";
export { stopRestoreWriteGuard } from "../client/restore-ownership";
import type { StopRunRecord } from "./stop-report";
import { clearPendingTeardown, pendingTeardownPathFor } from "../config/pending-teardown";

type RestoreRecord = Pick<StopRunRecord, "sharedTeardown" | "receiptClearFailed">;

export function refuseStopRestore(record: RestoreRecord): true {
  record.sharedTeardown = "refused";
  console.error("❌ Could not acquire the lock for shared configuration; restoration stopped and the obligation is kept. Rerun 'ocx stop'.");
  return true;
}

/** Only the named, discharged nonces are removed; C covers both ownership and unlink. */
export function clearRestoredStopReceipts(record: RestoreRecord, nonces: string[]): boolean {
  let entered = false;
  try {
    return withConfigMutationLockSync(() => {
      entered = true;
      let failed = false;
      for (const nonce of nonces) {
        if (!clearPendingTeardown(nonce)) {
          failed = true;
          record.receiptClearFailed = true;
          console.error(`❌ The shared teardown was discharged, but its receipt could not be removed: ${pendingTeardownPathFor(nonce)}`);
          console.error("   Remove it manually; otherwise every later stop will try to recover it again.");
        }
      }
      return failed;
    });
  } catch {
    if (entered) record.receiptClearFailed = true;
    return refuseStopRestore(record);
  }
}
