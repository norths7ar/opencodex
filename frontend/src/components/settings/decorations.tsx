import type { ReactNode, SVGProps } from "react";
import { HelpPopover } from "../help-popover";
export function QuestionIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <circle cx="12" cy="12" r="9.5" />
      <path d="M9.6 9.3a2.5 2.5 0 0 1 4.85.85c0 1.65-2.45 2.2-2.45 3.6" />
      <path d="M12 17h.01" />
    </svg>
  );
}

export function HelpTip({ label, children }: { label: string; children: ReactNode }) {
  return <HelpPopover label={label}>{children}</HelpPopover>;
}

/** Small neutral count pill beside a title. */
export function CountPill({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-[11px] font-medium text-fg-muted tabular-nums">
      {children}
    </span>
  );
}
