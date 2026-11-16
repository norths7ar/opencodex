import type { ReactNode } from "react";
import { Button } from "./button";

/** A known local location: present its purpose/name, never its absolute path. */
export function FileLocation({
  name,
  label,
  openLabel,
  busy,
  onOpen,
}: {
  name: string;
  label: ReactNode;
  openLabel: string;
  busy?: boolean;
  onOpen: () => void;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-3 px-4 py-3">
      <div className="min-w-0">
        <div className="text-[12px] text-fg-muted">{label}</div>
        <div className="break-words text-[13px] font-medium">{name}</div>
      </div>
      <Button size="sm" disabled={busy} aria-busy={busy} onClick={onOpen}>
        {openLabel}
      </Button>
    </div>
  );
}
