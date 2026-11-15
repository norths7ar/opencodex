import type { ReactNode } from "react";
import { useT } from "../../i18n/shared";
import { HelpPopover } from "../help-popover";

export function ModelsAbout({ text, children }: { text: string; children?: ReactNode }) {
  const t = useT();
  return (
    <HelpPopover label={t("nav.models")}>
      <p>{text}</p>
      {children}
    </HelpPopover>
  );
}
