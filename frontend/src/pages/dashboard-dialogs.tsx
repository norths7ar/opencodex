import type { useDashboardData } from "./use-dashboard-data";
import SubagentSurfaceWarningModal from "../components/SubagentSurfaceWarningModal";
import { SUBAGENT_SURFACE_GUIDE_URL } from "../subagent-surface";

type Dash = ReturnType<typeof useDashboardData>;
export function DashboardDialogs(d: Dash) {
  const {
    maAdvisory,
    maAdvisoryOpen,
    pendingMaMode,
    maBusy,
    keepMaMode,
    chooseMaV1,
    dismissMaSurfaceDialog,
  } = d;
  return (
    <>
      {(pendingMaMode || maAdvisoryOpen) && (
        <SubagentSurfaceWarningModal
          reason={pendingMaMode ? "selection" : "advisory"}
          mode={pendingMaMode ?? maAdvisory?.mode ?? "default"}
          docsUrl={maAdvisory?.docsUrl ?? SUBAGENT_SURFACE_GUIDE_URL}
          busy={maBusy}
          onContinue={() => {
            void keepMaMode();
          }}
          onChooseV1={() => {
            void chooseMaV1();
          }}
          onDismiss={dismissMaSurfaceDialog}
        />
      )}
    </>
  );
}
