import { cn } from "@/lib/utils";
const VARIANTS = {
  primary: "control-button-primary text-on-accent",
  outline:
    "bg-raised text-fg ring-1 ring-inset ring-line-strong hover:bg-accent-soft hover:text-accent",
  ghost: "bg-transparent text-fg-muted hover:bg-surface hover:text-fg",
  danger:
    "bg-danger-soft text-danger ring-1 ring-inset ring-danger/20 hover:bg-danger hover:text-on-accent",
} as const;

const SIZES = {
  // Size is explicit: neighbouring fields never silently change a button's height.
  sm: "h-[var(--ocx-control-h-sm)] gap-1.5 px-3 text-[13px]",
  md: "h-[var(--ocx-control-h)] gap-2 px-4 text-[13px]",
  icon: "size-[var(--ocx-control-h)] p-0",
} as const;

export type ButtonVariant = keyof typeof VARIANTS;
export type ButtonSize = keyof typeof SIZES;

export function buttonStyles(
  variant: ButtonVariant = "outline",
  size: ButtonSize = "md",
  className?: string,
) {
  return cn(
    "control-button ui-v2 inline-flex shrink-0 cursor-pointer items-center justify-center rounded-control font-medium leading-none whitespace-nowrap select-none",
    "transition-[background-color,color,box-shadow,scale] duration-200 ease-soft active:scale-[0.97] motion-reduce:transition-none motion-reduce:transform-none",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-highlight",
    "disabled:pointer-events-none disabled:opacity-50",
    "[&_svg]:size-4 [&_svg]:shrink-0",
    VARIANTS[variant],
    SIZES[size],
    className,
  );
}
