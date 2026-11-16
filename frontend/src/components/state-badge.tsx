import { cn } from "@/lib/utils";
import type { ReactNode } from "react";
export type Tone = "ok" | "warn" | "err" | "neutral";

const BADGE_TONE: Record<Tone, string> = {
  ok: "bg-success-soft text-success",
  warn: "bg-warning-soft text-warning",
  err: "bg-danger-soft text-danger",
  neutral: "bg-surface-2 text-fg-muted",
};

/** A state word in a soft pill with a dot (`integrations-6` status words). */
export function StateBadge({
  tone,
  children,
  className,
}: {
  tone: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "startup-badge inline-flex h-[22px] max-w-full shrink-0 items-center gap-1.5 rounded-full px-2 text-[12px] leading-4 font-semibold whitespace-nowrap",
        BADGE_TONE[tone],
        className,
      )}
    >
      <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current" />
      <span className="truncate">{children}</span>
    </span>
  );
}
