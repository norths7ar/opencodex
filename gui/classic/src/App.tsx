
import { useCallback, useEffect, useRef, useState } from "react";
import { useKeyedClientResource } from "./client-resource";
import Dashboard from "./pages/Dashboard";
import Providers from "./pages/Providers";
import Models from "./pages/Models";
import Subagents from "./pages/Subagents";
import Logs from "./pages/Logs";
import Usage from "./pages/Usage";
import Storage from "./pages/Storage";
import CodexSet from "./pages/CodexSet";
import Integrations from "./pages/Integrations";
import Startup from "./pages/Startup";
import ErrorBoundary from "./components/ErrorBoundary";
import QuotaSummaryBar from "./components/quota-summary-bar/QuotaSummaryBar";
import { IconMenu, IconGlobe, IconPower, IconX, IconRefresh, IconHistory } from "./icons";
import { switchDashboardEdition } from "./lib/dashboard-edition";
import { NAV_GROUPS, groupForPage, visibleGroupPages } from "./nav-groups";
import { SectionSwitcher } from "./components/SectionSwitcher";
import { useI18n, useT, LOCALES, localeDisplayName, type Locale, type TKey } from "./i18n/shared";
import { Select, ToastNotice, type NoticeTone } from "./ui";
import { configureApiTargets, DEVICE_PROOF_CHANGED_EVENT, hasApiSession, hasRememberedDashboardDevice, installApiAuthFetch, logoutApiSession, SESSION_CHANGED_EVENT, SESSION_UNAVAILABLE_EVENT } from "./api";
import { apiBaseForPlane, standaloneApiTargets } from "./api-targets";
import { type Page } from "./app-routing";
import { useAppRouteState } from "./use-app-route-state";
import { requestProxyStop } from "./stop-proxy";
import { useCodexRestart } from "./use-codex-restart";
import { confirmAction } from "./action-dialogs";
import { useSidebarCollapse } from "./use-sidebar-collapse";
import { ThemeSwitch, type ThemeMode } from "./components/theme-switch";
import { SidebarTopStrip } from "./components/app-titlebar";

type Theme = ThemeMode;

const PAGE_TKEY: Record<Page, TKey> = {
  dashboard: "nav.dashboard",
  startup: "nav.startup",
  providers: "nav.providers",
  models: "nav.models",
  subagents: "nav.subagents",
  logs: "nav.logs",
  usage: "nav.usage",
  storage: "nav.storage",
  "codex-set": "nav.codexSet",
  integrations: "nav.connect",
  claude: "nav.claude",
};

const API_BASE = import.meta.env.VITE_API_BASE || "";
const INITIAL_TARGETS = standaloneApiTargets(API_BASE);
configureApiTargets(INITIAL_TARGETS);
installApiAuthFetch();
const THEME_KEY = "ocx-theme";

function readRuntimeVersion(data: unknown): string | null {
  if (!data || typeof data !== "object" || !("version" in data)) return null;
  const version = (data as { version?: unknown }).version;
  return typeof version === "string" && version.length > 0 ? version : null;
}

function readStoredTheme(): Theme {
  const t = localStorage.getItem(THEME_KEY);
  return t === "light" || t === "dark" ? t : "system";
}

/*
 * The section switcher hides once only one member is left (Remote Workspace becoming
 * unavailable). If a switcher button had focus, hand it to this page's sidebar row when that
 * row is on screen, else to the main region, so keyboard focus never drops to <body>.
 */
function focusAfterSwitcher(): void {
  const row = document.querySelector<HTMLElement>(".sidebar .nav-item.active");
  const box = row?.getBoundingClientRect();
  const onScreen = !!box && box.width > 0 && box.height > 0
    && box.right > 0 && box.left < window.innerWidth
    && box.bottom > 0 && box.top < window.innerHeight;
  if (row && onScreen) {
    row.focus({ preventScroll: true });
    return;
  }
  const main = document.querySelector<HTMLElement>("main.main");
  if (!main) return;
  main.tabIndex = -1;
  main.focus({ preventScroll: true });
}

export default function App() {
  const { page, navigateToPage } = useAppRouteState();
  const [theme, setTheme] = useState<Theme>(readStoredTheme);
  const { locale, setLocale } = useI18n();
  const t = useT();
  const targets = INITIAL_TARGETS;
  const [sharedSessionReady, setSharedSessionReady] = useState(() => hasApiSession("shared"));
  const [sharedSessionEpoch, setSharedSessionEpoch] = useState(0);
  const [sessionLoggingOut, setSessionLoggingOut] = useState(false);
  /*
   * Results from the two sidebar orbs used to be `alert()`, which the app's webview draws
   * nowhere, so a refused stop and a completed one looked identical: nothing happened.
   * The toast is portaled, so reporting from the shell costs the page no layout.
   */
  const [actionFeedback, setActionFeedback] = useState<{ tone: NoticeTone; text: string } | null>(null);
  /** Bumped on every report so a repeated identical message restarts the dismiss timer. */
  const [feedbackRevision, setFeedbackRevision] = useState(0);
  const report = useCallback((text: string, tone: NoticeTone) => {
    setActionFeedback({ tone, text });
    setFeedbackRevision(revision => revision + 1);
  }, []);

  useEffect(() => {
    const unavailable = (event: Event) => {
      if ((event as CustomEvent<{ plane?: string }>).detail?.plane === "shared" && !hasApiSession("shared")) {
        setSharedSessionReady(false);
      }
    };
    // A change only ever reveals a session (Remember/Forget controls). Target discovery clears the
    // session transiently, and dropping readiness there would cancel the requests that recover it.
    const changed = (event: Event) => {
      if ((event as CustomEvent<{ plane?: string }>).detail?.plane === "shared" && hasApiSession("shared")) {
        setSharedSessionReady(true);
      }
    };
    window.addEventListener(SESSION_UNAVAILABLE_EVENT, unavailable);
    window.addEventListener(SESSION_CHANGED_EVENT, changed);
    const proofChanged = (event: Event) => {
      if ((event as CustomEvent<{ plane?: string }>).detail?.plane === "shared") setSharedSessionEpoch(epoch => epoch + 1);
    };
    window.addEventListener(DEVICE_PROOF_CHANGED_EVENT, proofChanged);
    return () => { window.removeEventListener(SESSION_UNAVAILABLE_EVENT, unavailable); window.removeEventListener(SESSION_CHANGED_EVENT, changed); window.removeEventListener(DEVICE_PROOF_CHANGED_EVENT, proofChanged); };
  }, []);
  const machineBase = apiBaseForPlane("machine", targets);
  const sharedBase = apiBaseForPlane("shared", targets);
  // The sidebar row that owns this page, and the members its switcher may offer.
  const activeGroup = groupForPage(page);
  const sectionPages = activeGroup ? visibleGroupPages(activeGroup) : [];
  // Claude is a tab inside Connect: both pages render one Integrations shell under one
  // boundary, so moving between Claude and another Connect tab keeps drafts and focus.
  const shellPage: Page = page === "claude" ? "integrations" : page;

  // Narrow screens: the sidebar becomes an off-canvas drawer behind a hamburger toggle.
  const [navOpen, setNavOpen] = useState(false);
  const { collapsed: navCollapsed, toggle: toggleNavCollapse } = useSidebarCollapse({ shortcut: true });
  const appRef = useRef<HTMLDivElement>(null);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const navWasOpen = useRef(false);

  useEffect(() => {
    // External navigation (hash edit, back/forward) also dismisses the mobile drawer.
    const dismissNav = () => setNavOpen(false);
    window.addEventListener("hashchange", dismissNav);
    window.addEventListener("popstate", dismissNav);
    return () => {
      window.removeEventListener("hashchange", dismissNav);
      window.removeEventListener("popstate", dismissNav);
    };
  }, []);

  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") { el.removeAttribute("data-theme"); localStorage.removeItem(THEME_KEY); }
    else { el.setAttribute("data-theme", theme); localStorage.setItem(THEME_KEY, theme); }
  }, [theme]);

  // Success expires on its own; a failure and a degraded result stay until the user
  // dismisses them, because those are the two the user has to act on.
  useEffect(() => {
    if (actionFeedback?.tone !== "ok") return;
    const timer = window.setTimeout(() => setActionFeedback(null), 4500);
    return () => window.clearTimeout(timer);
  }, [actionFeedback, feedbackRevision]);

  const healthPoll = useKeyedClientResource(
    `app-healthz:${machineBase}`,
    [machineBase],
    async (signal) => {
      const res = await fetch(`${machineBase}/healthz`, { signal });
      if (!res.ok) return null;
      return readRuntimeVersion(await res.json());
    },
    { pollMs: 30_000 },
  );

  const themeSwitch = <ThemeSwitch theme={theme} onChange={setTheme} />;
  const displayedVersion: string = healthPoll.data ?? __APP_VERSION__;

  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";         // no background scroll behind the drawer
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prevOverflow; };
  }, [navOpen]);

  // Move focus into the drawer on open; hand it back to the toggle on close.
  useEffect(() => {
    if (navOpen) {
      navWasOpen.current = true;
      // after the 180ms slide-in: while visibility is transitioning, focus() no-ops
      const timer = setTimeout(() => sidebarRef.current?.focus(), 200);
      return () => clearTimeout(timer);
    }
    if (navWasOpen.current) { navWasOpen.current = false; menuBtnRef.current?.focus(); }
  }, [navOpen]);

  // Growing the window past the breakpoint dismisses the drawer state.
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 761px)");
    const onChange = () => { if (mq.matches) setNavOpen(false); };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // The sidebar control is on every page, including Models. Bumping an epoch on a
  // settled restart lets the models tab re-read staleness without the two surfaces
  // sharing a controller — the backend is already single-flight, so what is missing
  // is invalidation, not mutual exclusion.
  const [codexRestartEpoch, setCodexRestartEpoch] = useState(0);
  const { restarting: codexRestarting, restart: handleCodexRestart } = useCodexRestart(sharedBase, {
    onSettled: () => setCodexRestartEpoch(epoch => epoch + 1),
    report,
  });

  const handleStop = async () => {
    const consented = await confirmAction({
      message: t("dash.stopConfirm"),
      confirmLabel: t("dash.stop"),
      tone: "danger",
    });
    if (!consented) return;
    setStopping(true);
    const outcome = await requestProxyStop(machineBase, {
      formatFailure: status => t("dash.stopFailed", { status: String(status) }),
      formatStillRunning: () => t("dash.stopStillRunning"),
      formatUnknown: () => t("dash.stopUnknown"),
    });
    /*
     * Only an accepted stop leaves the control pending, because the page is about to go
     * away with the server. A refusal and an unknown both mean the user is still here and
     * still looking at a running dashboard, so the control comes back either way.
     *
     * They are not reported the same, though. A refusal is the server's own answer and
     * reads as a failure; an unknown is the absence of an answer, and claiming either
     * success or failure there is the thing this lane exists to stop.
     */
    if (outcome.status !== "accepted") {
      setStopping(false);
      report(outcome.message, outcome.status === "rejected" ? "err" : "warn");
    }
  };

  const handleSessionLogout = async () => {
    if (sessionLoggingOut) return;
    setSessionLoggingOut(true);
    const loggedOut = await logoutApiSession("shared");
    setSessionLoggingOut(false);
    if (loggedOut) setSharedSessionReady(false);
    else report(t(hasRememberedDashboardDevice("shared") ? "connection.forgetDeviceFailed" : "connection.sessionLogoutFailed"), "err");
  };
  const deviceForgetAvailable = hasRememberedDashboardDevice("shared");
  const endSessionLabel = t(deviceForgetAvailable ? "connection.forgetDevice" : "connection.sessionLogout");

  /*
   * The brand is the control users reach for first when they want out of a deep page,
   * and it used to be an inert <div>: clicking the logo did nothing, so a user on
   * #providers had no obvious way back to the first screen. It is a button now.
   *
   * One node, two mount points (mobile topbar and drawer head), so both become
   * interactive from this single definition. `navigateToPage` is the deliberate-
   * navigation helper the nav rows use — it pushes a history entry, so Back still
   * returns to where the user came from — and closing the drawer is required because
   * the second mount lives inside it.
   */
  const brand = (
    <button
      type="button"
      className="brand brand-home"
      onClick={() => { navigateToPage("dashboard"); setNavOpen(false); }}
      aria-label={t("nav.goHome")}
      title={t("nav.goHome")}
      {...(page === "dashboard" ? { "aria-current": "page" as const } : {})}
    >
      <span className="brand-logo" role="img" aria-label={t("app.logoAria")} />
      <span className="name">opencodex</span>
      <span className="ver" title={displayedVersion}>v{displayedVersion}</span>
    </button>
  );

  const quotaSummary = page !== "startup" && (
    <ErrorBoundary key={sharedBase} pageName={t("quotaSummary.aria")} title={t("errorBoundary.title")} message={t("errorBoundary.message")} detailsLabel={t("errorBoundary.details")} reloadLabel={t("errorBoundary.reload")}>
      <QuotaSummaryBar apiBase={sharedBase} />
    </ErrorBoundary>
  );

  return (
    <div ref={appRef} className={`app${navCollapsed ? " app--nav-collapsed" : ""}`}>
      
      {actionFeedback && (
        <ToastNotice tone={actionFeedback.tone} onDismiss={() => setActionFeedback(null)} dismissLabel={t("common.close")}>
          {actionFeedback.text}
        </ToastNotice>
      )}
      {/* inert while the drawer is open: keeps focus and assistive tech inside the drawer */}
      <header className="mobile-topbar" inert={navOpen}>
        <button ref={menuBtnRef} type="button" className="menu-toggle" onClick={() => setNavOpen(o => !o)}
          aria-expanded={navOpen} aria-controls="app-sidebar"
          aria-label={t(navOpen ? "nav.closeMenu" : "nav.openMenu")} title={t(navOpen ? "nav.closeMenu" : "nav.openMenu")}>
          <IconMenu />
        </button>
        {brand}
        <div className="mobile-topbar-actions">
          {deviceForgetAvailable && sharedSessionReady && (
            <button type="button" className="sidebar-orb" onClick={() => { void handleSessionLogout(); }} disabled={sessionLoggingOut}
              aria-label={sessionLoggingOut ? t("connection.sessionLoggingOut") : endSessionLabel} title={endSessionLabel}>
              <IconX />
            </button>
          )}
          <button type="button" className="sidebar-orb sidebar-orb--danger" onClick={handleStop} disabled={stopping}
            aria-label={t("dash.stop")} title={t("dash.stop")}>
            <IconPower />
          </button>
          <button type="button" className="sidebar-orb"
            onClick={() => { void handleCodexRestart(); }} disabled={codexRestarting}
            aria-label={t("dash.codexRestart")} title={t("dash.codexRestart")}>
            <IconRefresh />
          </button>
        </div>
      </header>
      {navOpen && <div className="drawer-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      {/* Fixed to the window's top-left; kept outside .sidebar so the sidebar's
         backdrop-filter containing block can't clip it to 0 width when collapsed. */}
      <SidebarTopStrip collapsed={navCollapsed} onToggle={toggleNavCollapse} />
      <aside id="app-sidebar" className={`sidebar${navOpen ? " open" : ""}`} ref={sidebarRef} tabIndex={-1}>
        <div className="drawer-head">
          {brand}
          <button type="button" className="menu-toggle drawer-close" onClick={() => setNavOpen(false)}
            aria-label={t("nav.closeMenu")} title={t("nav.closeMenu")}>
            <IconX />
          </button>
        </div>
        <nav>
          {/*
            Codex Auth was once filtered out of this list whenever the workspace layout
            was active, on the grounds that the Providers workspace embeds the same
            account pool. It is now promoted to the second slot instead: there is only
            one layout, so that filter would have hidden the page permanently.
          */}
          {/*
            The sidebar is navigation only — no row owns a mutation. That rule was
            written when the Claude row carried the Claude Code connection switch;
            ClaudeCode owns GET/PUT /api/claude-code now, and the row itself is gone.
          */}
          {/*
            One row per navigation group (nav-groups.ts). A row lights up for any
            page it owns, so #logs/debug highlights Usage & Logs, and a click opens the
            group's first page.
          */}
          {NAV_GROUPS.map(group => {
            const { id, tkey, Icon } = group;
            const active = activeGroup?.id === id;
            return (
              <div key={id} className="nav-entry">
                <button type="button" className={`nav-item${active ? " active" : ""}`}
                  data-page={group.pages[0]}
                  data-group={id}
                  onClick={() => {
                    // Deliberate sidebar navigation — push a history entry.
                    navigateToPage(group.pages[0]);
                    setNavOpen(false);
                  }}
                  aria-current={active ? "page" : undefined}>
                  <Icon /> {t(tkey)}
                </button>
              </div>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          {/* Added to the classic snapshot: the way back to the current dashboard. */}
          <button type="button" className="nav-item" onClick={() => switchDashboardEdition("current")}
            title={t("shellV2.uiCurrentHint")}>
            <IconHistory /> {t("shellV2.uiCurrent")}
          </button>
          <div className="lang-toggle">
            <IconGlobe aria-hidden />
            <Select
              value={locale}
              options={LOCALES.map(l => ({ value: l.code, label: localeDisplayName(l.code) }))}
              onChange={v => setLocale(v as Locale)}
              label={t("lang.label")}
              placement="right"
              portal={false}
              style={{ flex: 1, minWidth: 0, width: "100%" }}
            />
          </div>
          <div className="sidebar-action-row sidebar-action-row--theme">
            <span className="sidebar-action-label">{t("theme.label")}</span>
            {themeSwitch}
          </div>
          <div className="sidebar-action-row">
            <span className="sidebar-action-label">{t("dash.actions")}</span>
            <div className="sidebar-action-orbs">
              {deviceForgetAvailable && sharedSessionReady && (
                <button type="button" className="sidebar-orb" onClick={() => { void handleSessionLogout(); }} disabled={sessionLoggingOut}
                  aria-label={sessionLoggingOut ? t("connection.sessionLoggingOut") : endSessionLabel}
                  title={endSessionLabel}>
                  <IconX />
                </button>
              )}
              <button type="button" className="sidebar-orb sidebar-orb--danger"
                onClick={handleStop} disabled={stopping}
                aria-label={stopping ? t("dash.stopping") : t("dash.stop")}
                title={stopping ? t("dash.stopping") : t("dash.stop")}>
                <IconPower />
              </button>
              <button type="button" className="sidebar-orb"
                onClick={() => { void handleCodexRestart(); }} disabled={codexRestarting}
                aria-label={codexRestarting ? t("dash.codexRestarting") : t("dash.codexRestart")}
                title={codexRestarting ? t("dash.codexRestarting") : t("dash.codexRestart")}>
                <IconRefresh />
              </button>
            </div>
          </div>
          
        </div>
      </aside>

      <main className="main" inert={navOpen}>
        {quotaSummary}
        <div className="main-inner">
          {/*
            Outside the page-keyed ErrorBoundary on purpose: that boundary remounts on
            every page change, which would destroy the button the user just activated
            and drop keyboard focus to <body>.
          */}
          {sectionPages.length > 1 && (
            <SectionSwitcher
              items={sectionPages.map(member => ({ page: member, label: t(PAGE_TKEY[member]) }))}
              currentPage={page}
              onNavigate={navigateToPage}
              ariaLabel={t("nav.sectionNavigation")}
              onFocusOrphaned={focusAfterSwitcher}
            />
          )}
          
          <ErrorBoundary
            key={shellPage}
            pageName={t(PAGE_TKEY[shellPage])}
            title={t("errorBoundary.title")}
            message={t("errorBoundary.message")}
            detailsLabel={t("errorBoundary.details")}
            reloadLabel={t("errorBoundary.reload")}
          >
                {page === "dashboard" && <Dashboard apiBase={sharedBase} refreshEpoch={sharedSessionEpoch} />}
                {page === "startup" && <Startup apiBase={sharedBase} />}
                {page === "providers" && <Providers apiBase={sharedBase} />}
                {page === "models" && <Models key={sharedBase} apiBase={sharedBase} restartEpoch={codexRestartEpoch} reportRestart={report} />}
                {page === "subagents" && <Subagents key={sharedBase} apiBase={sharedBase} />}
                
                {page === "logs" && <Logs apiBase={sharedBase} />}
                {page === "usage" && <Usage apiBase={sharedBase} />}
                {page === "storage" && <Storage apiBase={sharedBase} />}
                
                
                {page === "codex-set" && <CodexSet apiBase={sharedBase} />}
                {shellPage === "integrations" && <Integrations apiBase={sharedBase} />}
          </ErrorBoundary>
        </div>
      </main>
    </div>
  );
}
