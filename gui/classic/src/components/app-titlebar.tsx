/** Shared dashboard top strips keep sidebar controls aligned with the main header. */
import { IconPanelLeft } from "../icons";
import { useT } from "../i18n/shared";
import "./app-titlebar.css";

export function SidebarTopStrip({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const t = useT();
  const label = collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar");
  return (
    <div className="sidebar-top">
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

