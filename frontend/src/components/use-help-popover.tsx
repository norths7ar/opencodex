import { useId, useRef, useState, type ReactNode } from "react";
import { useT } from "../i18n/shared";
import { HelpSurface } from "./help-surface";

export function useHelpPopover(label: string) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const id = useId();
  const t = useT();
  const button = (
    <button
      ref={anchor}
      type="button"
      className="ocx-help-trigger"
      aria-label={t("subagentsV2.moreAbout", { label })}
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-controls={open ? id : undefined}
      onClick={() => setOpen((value) => !value)}
    >
      <svg
        viewBox="0 0 24 24"
        width="16"
        height="16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M9.6 9.3a2.5 2.5 0 0 1 4.85.85c0 1.65-2.45 2.2-2.45 3.6" />
        <path d="M12 17h.01" />
      </svg>
    </button>
  );
  const panel = (children: ReactNode, _className?: string) =>
    open ? (
      <HelpSurface id={id} label={label} anchor={anchor} onClose={() => setOpen(false)}>
        {children}
      </HelpSurface>
    ) : null;
  return { open, button, panel };
}
