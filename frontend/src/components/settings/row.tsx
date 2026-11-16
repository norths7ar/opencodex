import { useT } from "@/i18n/shared";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";
import { HelpPopover } from "../help-popover";
export interface SettingsRowProps {
  label: ReactNode;
  /** Wires a native <label> to the control (Select ids, inputs). Without it the label is text. */
  labelFor?: string;
  labelId?: string;
  /** One plain line under the label. */
  hint?: ReactNode;
  /** Longer explanation, opened from the row's help button. */
  details?: ReactNode;
  /** A "?" that opens something else (one of the page's help dialogs) instead of `details`. */
  help?: ReactNode;
  /** Small trailing marker after the label (a badge, a code chip). */
  badge?: ReactNode;
  control?: ReactNode;
  /** Full-width content below the row: notices, sub-options, status lines. */
  children?: ReactNode;
  /** Stack the control under the copy at every width (wide controls such as a source list). */
  stacked?: boolean;
  /** Keep a small control (a switch) on the right even on a phone. */
  inline?: boolean;
  className?: string;
  busy?: boolean;
}

export function SettingsRow({
  label,
  labelFor,
  labelId,
  hint,
  details,
  help,
  badge,
  control,
  children,
  stacked,
  inline,
  className,
  busy,
}: SettingsRowProps) {
  const t = useT();
  const labelText = typeof label === "string" ? label : undefined;
  const labelClass = "m-0 text-[14px] leading-5 font-medium text-fg";
  return (
    <div
      className={cn("settings-row @container flex min-w-0 flex-col gap-3 px-4 py-3.5", className)}
      aria-busy={busy || undefined}
    >
      <div
        className={cn(
          "flex min-w-0 flex-col gap-3",
          !stacked && "@sm:flex-row @sm:items-center @sm:justify-between @sm:gap-6",
          inline && "flex-row items-center justify-between gap-4",
        )}
      >
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex min-h-[22px] flex-wrap items-center gap-x-1.5 gap-y-1">
            {labelFor ? (
              <label htmlFor={labelFor} id={labelId} className={labelClass}>
                {label}
              </label>
            ) : (
              <span id={labelId} className={labelClass}>
                {label}
              </span>
            )}
            {details && (
              <HelpPopover label={labelText ?? t("proxyV2.details")}>{details}</HelpPopover>
            )}
            {help}
            {badge}
          </div>
          {hint && (
            <p className="m-0 max-w-[62ch] text-[13px] leading-[18px] text-fg-muted">{hint}</p>
          )}
        </div>
        {control && (
          <div
            className={cn(
              "settings-row-control flex min-w-0 flex-wrap items-center gap-2",
              !stacked && "@sm:shrink-0 @sm:justify-end",
              inline && "shrink-0",
            )}
          >
            {control}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}
