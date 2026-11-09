import type { ResponsesSendBudget } from "./request-send-budget";
import type { SingleUseDispatchPermit } from "../../lib/request-execution-budget";

/** Track reserved hosted-inference credential hops until dispatch or refund. */
export function createSidecarSendBudget(
  budget: Pick<ResponsesSendBudget, "pendingHopPermit">,
) {
  let hopPermit: SingleUseDispatchPermit | undefined;
  /** Detach this owner’s hop before refunding its still-unused reservation. */
  const releaseHop = (): void => {
    const permit = hopPermit;
    hopPermit = undefined;
    if (budget.pendingHopPermit === permit) budget.pendingHopPermit = undefined;
    permit?.release();
  };
  return {
    /** Retain a hop only when its caller defers settlement until dispatch. */
    ownCredentialHop(permit?: SingleUseDispatchPermit, retainUntilDispatch = false): void {
      if (!retainUntilDispatch) { permit?.use(); return; } // Preserve direct callers' reporting contract.
      releaseHop();
      hopPermit = permit;
      budget.pendingHopPermit = permit;
    },
    releaseUnsentHop: releaseHop,
  };
}
