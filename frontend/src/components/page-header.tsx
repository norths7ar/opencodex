import { useContext, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { PageHeaderTarget } from "./page-header-context";

/** Pages supply content; the application shell owns placement above section navigation. */
export function PageHeader({
  title,
  subtitle,
  actions,
  titleId,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  titleId?: string;
}) {
  const target = useContext(PageHeaderTarget);
  const header = (
    <header className="console-page-header">
      <div className="console-page-heading">
        <h2 id={titleId}>{title}</h2>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions && <div className="console-page-actions">{actions}</div>}
    </header>
  );
  return target ? createPortal(header, target) : header;
}
