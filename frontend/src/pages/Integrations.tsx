import { PageHeader } from "../components/page-header";
import { Fragment, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { navigateHash } from "../hash-routing";
import { canonicalHashPath } from "../app-routing";
import { useT } from "../i18n/shared";
import ClientMark from "../components/ClientMark";
import { INTEGRATION_MARKS } from "../components/integration-marks";
import IntegrationsOverview from "./integrations/IntegrationsOverview";
import FileIntegrationPage from "./integrations/FileIntegrationPage";
import { TABS, type IntegrationTab } from "./integrations/integration-tabs";
import CodexConnection from "./integrations/CodexConnection";

function readIntegrationTab(hash = window.location.hash): IntegrationTab {
  // Read the destination, not a legacy spelling the route hook is about to replace.
  const raw = canonicalHashPath(hash);
  const match = TABS.find((tab) => tab.hash === raw);
  return match?.id ?? "overview";
}

function tabDomId(tab: IntegrationTab): string {
  return `integrations-tab-${tab}`;
}

function panelDomId(tab: IntegrationTab): string {
  return `integrations-panel-${tab}`;
}

function tabMark(tab: IntegrationTab): string | null {
  if (tab === "overview") return null;
  return INTEGRATION_MARKS[tab] ?? null;
}

export default function Integrations({ apiBase }: { apiBase: string }) {
  const t = useT();
  const tabs = TABS;
  const [tab, setTab] = useState<IntegrationTab>(readIntegrationTab);
  const visibleTab = tab;
  /*
   * Panels mount lazily and stay mounted so a draft survives a tab hop. Each panel is gated
   * by `active`, which is what stops a hidden one from polling.
   */
  const [mounted, setMounted] = useState<ReadonlySet<IntegrationTab>>(
    () => new Set([readIntegrationTab()]),
  );
  const tabRefs = useRef<Map<IntegrationTab, HTMLButtonElement> | null>(null);
  if (tabRefs.current === null) tabRefs.current = new Map();

  /*
   * Every tab change goes through here, whether it came from a click or from
   * the browser's own history. Accumulating the mounted set in an effect
   * instead would run a second render pass after every switch for a value
   * both callers already know.
   */
  const activateTab = (next: IntegrationTab) => {
    setTab(next);
    setMounted((current) => (current.has(next) ? current : new Set([...current, next])));
  };

  useEffect(() => {
    const syncFromHash = () => activateTab(readIntegrationTab());
    window.addEventListener("hashchange", syncFromHash);
    window.addEventListener("popstate", syncFromHash);
    return () => {
      window.removeEventListener("hashchange", syncFromHash);
      window.removeEventListener("popstate", syncFromHash);
    };
  }, []);

  const selectTab = (next: IntegrationTab, moveFocus: boolean) => {
    const definition = TABS.find((candidate) => candidate.id === next);
    if (!definition) return;
    navigateHash(definition.hash);
    activateTab(next);
    if (moveFocus) {
      window.requestAnimationFrame(() => {
        tabRefs.current!.get(next)?.focus({ preventScroll: true });
      });
    }
  };

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const index = tabs.findIndex((candidate) => candidate.id === visibleTab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    selectTab(tabs[nextIndex].id, true);
  };

  return (
    <section className="integrations-page">
      <PageHeader title={t("nav.connect")} />
      <div
        className="page-tabs connect-tabs"
        role="tablist"
        aria-label={t("integrations.tabsLabel")}
      >
        {tabs.map((definition) => (
          <Fragment key={definition.id}>
            <button
              ref={(node) => {
                if (node) tabRefs.current!.set(definition.id, node);
                else tabRefs.current!.delete(definition.id);
              }}
              type="button"
              role="tab"
              id={tabDomId(definition.id)}
              aria-selected={visibleTab === definition.id}
              aria-controls={panelDomId(definition.id)}
              tabIndex={visibleTab === definition.id ? 0 : -1}
              className={`page-tab${visibleTab === definition.id ? " page-tab--active" : ""}`}
              onClick={() => selectTab(definition.id, true)}
              onKeyDown={handleTabKeyDown}
            >
              {definition.id !== "overview" && (
                <ClientMark src={tabMark(definition.id)} label={t(definition.labelKey)} size={14} />
              )}
              {t(definition.labelKey)}
            </button>
          </Fragment>
        ))}
      </div>

      {tabs.map((definition) => {
        if (!mounted.has(definition.id) && definition.id !== visibleTab) return null;
        const active = visibleTab === definition.id;
        return (
          <div
            key={definition.id}
            role="tabpanel"
            id={panelDomId(definition.id)}
            aria-labelledby={tabDomId(definition.id)}
            hidden={!active}
          >
            {definition.id === "overview" && (
              <IntegrationsOverview apiBase={apiBase} active={active} />
            )}
            {definition.id === "codex" && <CodexConnection apiBase={apiBase} active={active} />}
            {definition.id === "dsh" && (
              <FileIntegrationPage apiBase={apiBase} client="dsh" active={active} />
            )}
          </div>
        );
      })}
    </section>
  );
}
