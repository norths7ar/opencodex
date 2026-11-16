/** Shared dashboard top strips keep sidebar controls aligned with the main header. */
import type { ReactNode } from "react";
import { useT } from "../i18n/shared";
import { IconPanelLeft } from "../icons";
import "./app-titlebar.css";

/**
 * `brand` is the wordmark the strip shows before the toggle, so the collapse control sits
 * beside the name it hides. It is hidden while collapsed (only the toggle floats then).
 */
export function SidebarTopStrip({
  collapsed,
  onToggle,
  brand,
}: {
  collapsed: boolean;
  onToggle: () => void;
  brand?: ReactNode;
}) {
  const t = useT();
  const label = collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar");
  return (
    <div className="sidebar-top">
      {brand}
      <button
        type="button"
        className="sidebar-collapse"
        onClick={onToggle}
        aria-expanded={!collapsed}
        aria-controls="app-sidebar"
        aria-label={label}
        title={label}
      >
        <IconPanelLeft />
      </button>
    </div>
  );
}
