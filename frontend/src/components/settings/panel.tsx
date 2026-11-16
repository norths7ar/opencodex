import { cn } from "@/lib/utils";
import { useId, type ReactNode } from "react";
interface PanelProps {
  id?: string;
  title: ReactNode;
  /** One line under the title. Longer text goes behind `help`. */
  description?: ReactNode;
  /** Items beside the title: a count, a "?" button. */
  meta?: ReactNode;
  /** Controls on the right of the head. */
  actions?: ReactNode;
  /** Portal content; never participates in the frame's layout. */
  helpOverlay?: ReactNode;
  children?: ReactNode;
  className?: string;
  headClassName?: string;
  busy?: boolean;
}

/** Panel with a header, a shared content surface and an optional footer. */
export function Panel({
  id,
  title,
  description,
  meta,
  actions,
  helpOverlay,
  children,
  className,
  headClassName,
  busy,
}: PanelProps) {
  const titleId = useId();
  return (
    <section
      id={id}
      aria-labelledby={titleId}
      aria-busy={busy}
      className={cn(
        "flex min-w-0 scroll-mt-[72px] flex-col overflow-hidden rounded-[14px] bg-raised shadow-sm ring-1 ring-inset ring-line",
        className,
      )}
    >
      <div
        className={cn(
          "flex min-w-0 flex-wrap items-start gap-x-3 gap-y-2 border-b border-line bg-surface/60 px-5 py-4",
          headClassName,
        )}
      >
        <div className="flex min-w-[min(100%,15rem)] flex-1 flex-col gap-0.5">
          <div className="flex min-w-0 items-center gap-2">
            <h3 id={titleId} className="m-0 text-[15px] leading-5 font-semibold text-fg">
              {title}
            </h3>
            {meta}
          </div>
          {description && <p className="m-0 text-[13px] leading-5 text-fg-muted">{description}</p>}
        </div>
        {actions && (
          <div className="flex shrink-0 flex-wrap items-center gap-2 max-sm:w-full">{actions}</div>
        )}
      </div>
      {helpOverlay}
      {children}
    </section>
  );
}

/** Panel content shares its parent's surface; rows provide their own spacing. */
export function PanelBody({
  children,
  className,
  as: Tag = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "ol" | "ul";
}) {
  return (
    <Tag className={cn("m-0 flex min-w-0 list-none flex-col divide-y divide-line p-0", className)}>
      {children}
    </Tag>
  );
}

/** Status on the left, actions on the right. */
export function PanelFooter({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "flex min-h-12 flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 text-[13px] text-fg-muted",
        className,
      )}
    >
      {children}
    </div>
  );
}
