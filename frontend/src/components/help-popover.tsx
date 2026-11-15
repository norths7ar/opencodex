import type { ReactNode } from "react";
import { useHelpPopover } from "./use-help-popover";

export function HelpPopover({ label, children }: { label: string; children: ReactNode }) {
  const help = useHelpPopover(label);
  return (
    <>
      {help.button}
      {help.panel(children)}
    </>
  );
}
