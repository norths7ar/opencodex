import { useI18n } from "../../i18n/shared";
import { liteTranslator } from "../../i18n/lite-copy";
import type { ConsequenceCopy } from "./ConsequenceDialog";

/** Python restores owned configuration and catalog files without changing thread identity. */
export function useCodexRestoreCopy(): (path: string) => ConsequenceCopy {
  const { locale } = useI18n();
  const t = liteTranslator(locale);
  return (path) => ({
    titleKey: "integrations.dialog.codex.title",
    changesKey: "integrations.dialog.codex.changes",
    breakageKey: "integrations.dialog.codex.breakage",
    undoKey: "integrations.dialog.codex.undo",
    sideEffectKey: "integrations.dialog.codex.sideEffect",
    confirmKey: "integrations.dialog.codex.confirm",
    vars: { path },
    text: {
      changes: t("codexRestoreChanges", { path }),
      breakage: t("codexRestoreBreakage"),
      undo: t("codexRestoreUndo"),
      sideEffect: t("codexRestoreSideEffect"),
    },
  });
}
