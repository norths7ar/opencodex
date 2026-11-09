import { withConfigMutationLockSync } from "../config";

export type RestoreBoundaryOwnership = "restore" | "unproven";
export type RestoreOwnershipRefusal = Exclude<RestoreBoundaryOwnership, "restore">;

export class StopRestoreOwnershipError extends Error {
  constructor(readonly boundary: RestoreOwnershipRefusal) {
    super(`stop_restore_${boundary}`);
  }
}

/** Serialize each restore write with config mutations; no lease crosses an await. */
export function stopRestoreWriteGuard() {
  let boundary: RestoreBoundaryOwnership = "restore";
  return {
    boundary: () => boundary,
    run<T>(write: () => T): T {
      let entered = false;
      try {
        return withConfigMutationLockSync(() => {
          entered = true;
          if (boundary !== "restore") throw new StopRestoreOwnershipError(boundary);
          return write();
        });
      } catch (error) {
        if (!entered) boundary = "unproven";
        else if (error instanceof StopRestoreOwnershipError) boundary = error.boundary;
        throw error;
      }
    },
  };
}
