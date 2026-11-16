import { useT } from "@/i18n/shared";
import { cn } from "@/lib/utils";
import { Copy01Icon, Tick02Icon } from "@hugeicons/core-free-icons";
import { HugeiconsIcon } from "@hugeicons/react";
import type { ReactNode } from "react";
/** A bare icon button that shows a soft plate only on hover. */
export function IconButton({
  label,
  onClick,
  children,
  disabled,
  tone = "neutral",
  className,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  disabled?: boolean;
  tone?: "neutral" | "danger";
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "ui-v2 inline-grid size-[var(--ocx-control-h-sm)] shrink-0 cursor-pointer place-items-center rounded-[8px] text-fg-muted transition-colors",
        "hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-highlight",
        "disabled:pointer-events-none disabled:opacity-40 [&_svg]:size-4",
        tone === "danger" && "hover:bg-danger-soft hover:text-danger",
        className,
      )}
    >
      {children}
    </button>
  );
}

/** Copy-to-clipboard icon button; the icon turns into a tick while the copy is fresh. */
export function CopyButton({
  copied,
  onCopy,
  className,
}: {
  copied: boolean;
  onCopy: () => void;
  className?: string;
}) {
  const t = useT();
  return (
    <IconButton
      label={t(copied ? "startup.copied" : "startup.copy")}
      onClick={onCopy}
      className={className}
    >
      <HugeiconsIcon
        icon={copied ? Tick02Icon : Copy01Icon}
        size={16}
        strokeWidth={1.8}
        aria-hidden="true"
        className={copied ? "text-success" : undefined}
      />
    </IconButton>
  );
}

/** One command in a copy list: a plain-words label, the command in mono, a copy button. */
export function CommandRow({
  label,
  command,
  copied,
  onCopy,
}: {
  label?: ReactNode;
  command: string;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <div className="flex min-w-0 items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {label && <span className="text-[13px] leading-5 font-medium text-fg">{label}</span>}
        <code className="block min-w-0 font-mono text-[12.5px] leading-5 text-fg-muted [overflow-wrap:anywhere]">
          {command}
        </code>
      </div>
      <CopyButton copied={copied} onCopy={onCopy} />
    </div>
  );
}
