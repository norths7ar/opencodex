import { Notice } from "./ui";
import { Button } from "./components/button";

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { confirmAction } from "./action-dialogs";
import { installApiAuthFetch } from "./api";
import { type Page } from "./app-routing";
import { useKeyedClientResource } from "./client-resource";
import ErrorBoundary from "./components/ErrorBoundary";
import { DataSurfaceSkeleton } from "./components/data-surface";
import { SectionSwitcher } from "./components/SectionSwitcher";
import { SidebarTopStrip } from "./components/app-titlebar";
import { PageHeaderTarget } from "./components/page-header-context";
import { PageHeader } from "./components/page-header";
import QuotaSummaryBar from "./components/quota-summary-bar/QuotaSummaryBar";
import { SidebarNav } from "./components/sidebar-nav";
import { ThemeSwitch, type ThemeMode } from "./components/theme-switch";
import { BrandMark } from "./components/ui-v2/brand-mark";
import { ProxyControls } from "./components/ui-v2/proxy-controls";
import { localeDisplayName, LOCALES, useI18n, useT, type Locale, type TKey } from "./i18n/shared";
import { IconGlobe, IconMenu, IconPower, IconRefresh, IconX } from "./icons";
import { proxyHealthOnline } from "./lib/proxy-health";
import { groupForPage, visibleGroupPages } from "./nav-groups";
import Home from "./pages/Home";
import { RuntimeCapabilitiesContext, type RuntimeCapabilities } from "./runtime-capabilities";
import { requestProxyStop } from "./stop-proxy";
import { Select, ToastNotice, type NoticeTone } from "./ui";
import { useAppRouteState } from "./use-app-route-state";
import { useCodexRestart } from "./use-codex-restart";
import { useSidebarCollapse } from "./use-sidebar-collapse";
import { pageImport } from "./page-loader";

// The shell and Home are immediate; other business screens load on first visit.
// Keep shared controls in the normal module graph rather than forced vendor chunks.
const Dashboard = lazy(pageImport(() => import("./pages/Dashboard")));
const Integrations = lazy(pageImport(() => import("./pages/Integrations")));
const Logs = lazy(pageImport(() => import("./pages/Logs")));
const Models = lazy(pageImport(() => import("./pages/Models")));
const Providers = lazy(pageImport(() => import("./pages/Providers")));
const Subagents = lazy(pageImport(() => import("./pages/Subagents")));
const Usage = lazy(pageImport(() => import("./pages/Usage")));
const LiteStorage = lazy(pageImport(() => import("./pages/LiteStorage")));

type Theme = ThemeMode;

const PAGE_TKEY: Record<Page, TKey> = {
  home: "shellV2.navHome",
  // The old dashboard is now the "Proxy details" member of the Advanced row.
  dashboard: "shellV2.pageProxyDetails",
  providers: "nav.providers",
  models: "nav.models",
  subagents: "nav.subagents",
  logs: "nav.logs",
  usage: "nav.usage",
  storage: "nav.storage",
  integrations: "nav.connect",
};

const API_BASE = import.meta.env.VITE_API_BASE || "";
installApiAuthFetch(API_BASE);
const THEME_KEY = "ocx-theme";

function readRuntimeVersion(data: unknown): string | null {
  if (!data || typeof data !== "object" || !("version" in data)) return null;
  const version = (data as { version?: unknown }).version;
  return typeof version === "string" && version.length > 0 ? version : null;
}

/** Seconds the proxy has been up, from the same /healthz body; null when it does not say. */
function readUptime(data: unknown): number | null {
  const uptime =
    data && typeof data === "object" ? (data as { uptime?: unknown }).uptime : undefined;
  return typeof uptime === "number" && Number.isFinite(uptime) && uptime >= 0 ? uptime : null;
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
  const onScreen =
    !!box &&
    box.width > 0 &&
    box.height > 0 &&
    box.right > 0 &&
    box.left < window.innerWidth &&
    box.bottom > 0 &&
    box.top < window.innerHeight;
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
  const [headerTarget, setHeaderTarget] = useState<HTMLDivElement | null>(null);
  const { page, navigateToPage } = useAppRouteState();
  const [theme, setTheme] = useState<Theme>(readStoredTheme);
  const { locale, setLocale, localeLoading, localeError } = useI18n();
  const [localeErrorDismissed, setLocaleErrorDismissed] = useState(false);
  const t = useT();
  /*
   * Results from the two sidebar orbs used to be `alert()`, which the app's webview draws
   * nowhere, so a refused stop and a completed one looked identical: nothing happened.
   * The toast is portaled, so reporting from the shell costs the page no layout.
   */
  const [actionFeedback, setActionFeedback] = useState<{ tone: NoticeTone; text: string } | null>(
    null,
  );
  /** Bumped on every report so a repeated identical message restarts the dismiss timer. */
  const [feedbackRevision, setFeedbackRevision] = useState(0);
  const report = useCallback((text: string, tone: NoticeTone) => {
    setActionFeedback({ tone, text });
    setFeedbackRevision((revision) => revision + 1);
  }, []);

  const capabilities = useKeyedClientResource<RuntimeCapabilities | null>(
    `runtime-capabilities:${API_BASE}`,
    [API_BASE],
    async (signal) => {
      const response = await fetch(`${API_BASE}/api/capabilities`, { signal });
      if (!response.ok) throw new Error(t("prov.loadConfigFail"));
      const result: RuntimeCapabilities = await response.json();
      if (!Array.isArray(result?.pages)) throw new Error(t("prov.loadConfigFail"));
      return result;
    },
  );
  const available = capabilities.data?.pages.includes(page) === true;
  // The sidebar row that owns this page, and the members its switcher may offer.
  const activeGroup = groupForPage(page);
  const sidebarPageAvailable = useCallback(
    (candidate: Page) => capabilities.data?.pages.includes(candidate) === true,
    [capabilities.data],
  );
  const sectionPages = activeGroup
    ? visibleGroupPages(activeGroup).filter(sidebarPageAvailable)
    : [];
  const shellPage = page;

  // Narrow screens: the sidebar becomes an off-canvas drawer behind a hamburger toggle.
  const [navOpen, setNavOpen] = useState(false);
  const { collapsed: navCollapsed, toggle: toggleNavCollapse } = useSidebarCollapse({
    shortcut: true,
  });
  const appRef = useRef<HTMLDivElement>(null);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  // Menu search and row shortcuts live in SidebarNav; App only says how to show the rail
  // (expand the collapsed rail, open the phone drawer) and how to navigate.
  const revealSidebar = useCallback(() => {
    if (navCollapsed) toggleNavCollapse();
    if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 760px)").matches)
      setNavOpen(true);
  }, [navCollapsed, toggleNavCollapse]);
  // Deliberate sidebar navigation — push a history entry and close the phone drawer.
  const sidebarNavigate = (target: Page) => {
    navigateToPage(target);
    setNavOpen(false);
  };
  const sidebarPageLabel = (target: Page) => t(PAGE_TKEY[target]);
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
    if (theme === "system") {
      el.removeAttribute("data-theme");
      localStorage.removeItem(THEME_KEY);
    } else {
      el.setAttribute("data-theme", theme);
      localStorage.setItem(THEME_KEY, theme);
    }
  }, [theme]);

  // Success expires on its own; a failure and a degraded result stay until the user
  // dismisses them, because those are the two the user has to act on.
  useEffect(() => {
    if (actionFeedback?.tone !== "ok") return;
    const timer = window.setTimeout(() => setActionFeedback(null), 4500);
    return () => window.clearTimeout(timer);
  }, [actionFeedback, feedbackRevision]);

  const healthPoll = useKeyedClientResource(
    `app-healthz:${API_BASE}`,
    [API_BASE],
    async (signal) => {
      const res = await fetch(`${API_BASE}/healthz`, { signal });
      if (!res.ok) throw new Error("Health check failed");
      const body: unknown = await res.json();
      return { version: readRuntimeVersion(body), uptime: readUptime(body) };
    },
    { pollMs: 30_000 },
  );

  const themeSwitch = <ThemeSwitch theme={theme} onChange={setTheme} />;
  const displayedVersion: string = healthPoll.data?.version ?? __APP_VERSION__;
  // A first read is unknown; only a completed health check establishes connectivity.
  const healthPending = healthPoll.data === undefined && healthPoll.error === undefined;
  const proxyOnline = proxyHealthOnline(healthPoll.data, healthPoll.error);
  const uptime = proxyOnline ? (healthPoll.data?.uptime ?? null) : null;
  // The sidebar row is narrow, so it shows the time alone; the status word is its tooltip.
  const uptimeLabel =
    uptime === null
      ? undefined
      : t("shellV2.uptimeValue", {
          h: String(Math.floor(uptime / 3600)),
          m: String(Math.floor((uptime % 3600) / 60)),
        });

  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden"; // no background scroll behind the drawer
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [navOpen]);

  // Move focus into the drawer on open; hand it back to the toggle on close.
  useEffect(() => {
    if (navOpen) {
      navWasOpen.current = true;
      // after the 180ms slide-in: while visibility is transitioning, focus() no-ops
      // Menu search (Cmd/Ctrl+K) opens the drawer and focuses its own field; keep that focus.
      const timer = setTimeout(() => {
        if (!sidebarRef.current?.contains(document.activeElement)) sidebarRef.current?.focus();
      }, 200);
      return () => clearTimeout(timer);
    }
    if (navWasOpen.current) {
      navWasOpen.current = false;
      menuBtnRef.current?.focus();
    }
  }, [navOpen]);

  // Growing the window past the breakpoint dismisses the drawer state.
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 761px)");
    const onChange = () => {
      if (mq.matches) setNavOpen(false);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // The sidebar control is on every page, including Models. Bumping an epoch on a
  // settled restart lets the models tab re-read staleness without the two surfaces
  // sharing a controller — the backend is already single-flight, so what is missing
  // is invalidation, not mutual exclusion.
  const [codexRestartEpoch, setCodexRestartEpoch] = useState(0);
  const { restarting: codexRestarting, restart: handleCodexRestart } = useCodexRestart(API_BASE, {
    onSettled: () => setCodexRestartEpoch((epoch) => epoch + 1),
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
    const outcome = await requestProxyStop(API_BASE, {
      formatFailure: (status) => t("dash.stopFailed", { status: String(status) }),
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
  const homePage: Page = sidebarPageAvailable("home") ? "home" : "providers";
  const homeLabel = homePage === "home" ? t("nav.goHome") : t(PAGE_TKEY[homePage]);
  const brand = (
    <button
      type="button"
      className="brand brand-home"
      onClick={() => {
        navigateToPage(homePage);
        setNavOpen(false);
      }}
      aria-label={homeLabel}
      title={homeLabel}
      {...(page === homePage ? { "aria-current": "page" as const } : {})}
    >
      <BrandMark className="ui-v2 brand-logo-v2 h-[24px] w-[27px]" label={t("app.logoAria")} />
      {/* Product name is identical in every locale. */}
      {/* eslint-disable-next-line local-i18n/no-hardcoded-ui-strings */}
      <span className="name">OpenCodex Lite</span>
      <span className="ver" title={displayedVersion}>
        v{displayedVersion}
      </span>
    </button>
  );
  // The wide-screen strip carries the wordmark beside the collapse toggle; the version moves
  // to the foot there, so this copy has none. The topbar and drawer keep `brand` above.
  const stripBrand = (
    <button
      type="button"
      className="brand brand-home brand--strip"
      onClick={() => navigateToPage(homePage)}
      aria-label={homeLabel}
      title={homeLabel}
      {...(page === homePage ? { "aria-current": "page" as const } : {})}
    >
      <BrandMark className="ui-v2 brand-logo-v2 h-[22px] w-[25px]" label={t("app.logoAria")} />
      {/* Product name is identical in every locale. */}
      {/* eslint-disable-next-line local-i18n/no-hardcoded-ui-strings */}
      <span className="name">OpenCodex Lite</span>
    </button>
  );

  const quotaSummary = (!capabilities.data || capabilities.data.quota) && !capabilities.loading && (
    <ErrorBoundary
      key={`quota:${API_BASE}`}
      pageName={t("quotaSummary.aria")}
      title={t("errorBoundary.title")}
      message={t("errorBoundary.message")}
      detailsLabel={t("errorBoundary.details")}
      reloadLabel={t("errorBoundary.reload")}
    >
      <QuotaSummaryBar apiBase={API_BASE} />
    </ErrorBoundary>
  );

  return (
    <RuntimeCapabilitiesContext.Provider value={capabilities.data ?? null}>
      <div ref={appRef} className={`app${navCollapsed ? " app--nav-collapsed" : ""}`}>
        {actionFeedback ? (
          <ToastNotice
            tone={actionFeedback.tone}
            onDismiss={() => setActionFeedback(null)}
            dismissLabel={t("common.close")}
          >
            {actionFeedback.text}
          </ToastNotice>
        ) : (
          localeError &&
          !localeErrorDismissed && (
            <ToastNotice
              tone="err"
              onDismiss={() => setLocaleErrorDismissed(true)}
              dismissLabel={t("common.close")}
            >
              {t("lang.loadFailed")}
            </ToastNotice>
          )
        )}
        {/* inert while the drawer is open: keeps focus and assistive tech inside the drawer */}
        <header className="mobile-topbar" inert={navOpen}>
          <button
            ref={menuBtnRef}
            type="button"
            className="menu-toggle"
            onClick={() => setNavOpen((o) => !o)}
            aria-expanded={navOpen}
            aria-controls="app-sidebar"
            aria-label={t(navOpen ? "nav.closeMenu" : "nav.openMenu")}
            title={t(navOpen ? "nav.closeMenu" : "nav.openMenu")}
          >
            <IconMenu />
          </button>
          {brand}
          <div className="mobile-topbar-actions">
            <button
              type="button"
              className="sidebar-orb sidebar-orb--danger"
              onClick={handleStop}
              disabled={stopping}
              aria-label={t("dash.stop")}
              title={t("dash.stop")}
            >
              <IconPower />
            </button>
            <button
              type="button"
              className="sidebar-orb"
              onClick={() => {
                void handleCodexRestart();
              }}
              disabled={codexRestarting}
              aria-label={t("dash.codexRestart")}
              title={t("dash.codexRestart")}
            >
              <IconRefresh />
            </button>
          </div>
        </header>
        {navOpen && (
          <div className="drawer-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />
        )}
        {/* Fixed to the window's top-left; kept outside .sidebar so the sidebar's
         backdrop-filter containing block can't clip it to 0 width when collapsed. */}
        <SidebarTopStrip collapsed={navCollapsed} onToggle={toggleNavCollapse} brand={stripBrand} />
        <aside
          id="app-sidebar"
          className={`sidebar${navOpen ? " open" : ""}`}
          ref={sidebarRef}
          tabIndex={-1}
        >
          <div className="drawer-head">
            {brand}
            <button
              type="button"
              className="menu-toggle drawer-close"
              onClick={() => setNavOpen(false)}
              aria-label={t("nav.closeMenu")}
              title={t("nav.closeMenu")}
            >
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
            <SidebarNav
              activeGroupId={activeGroup?.id ?? null}
              pageLabel={sidebarPageLabel}
              pageAvailable={sidebarPageAvailable}
              onNavigate={sidebarNavigate}
              onReveal={revealSidebar}
              t={t}
            />
          </nav>
          <div className="sidebar-foot">
            <ProxyControls
              label={t("dash.actions")}
              statusLabel={t(
                healthPending ? "common.loading" : proxyOnline ? "dash.online" : "dash.offline",
              )}
              online={proxyOnline}
              detail={uptimeLabel}
              stopLabel={t("dash.stop")}
              stoppingLabel={t("dash.stopping")}
              restartLabel={t("dash.codexRestart")}
              restartingLabel={t("dash.codexRestarting")}
              leverHint={t("shellV2.leverHint", {
                restart: t("dash.codexRestart"),
                stop: t("dash.stop"),
              })}
              leverText={{
                hover: t("shellV2.leverHover"),
                cancel: t("shellV2.leverCancel"),
                restart: t("shellV2.leverRelease", { action: t("shellV2.leverReload") }),
                stop: t("shellV2.leverRelease", { action: t("shellV2.leverStop") }),
              }}
              stopping={stopping}
              restarting={codexRestarting}
              onStop={handleStop}
              onRestart={() => {
                void handleCodexRestart();
              }}
            />
            {/* Display preferences share one row: the language picker, then the theme orb. */}
            <div className="sidebar-pref-row">
              <div
                className="lang-toggle"
                aria-busy={localeLoading}
                title={localeLoading ? t("common.loading") : undefined}
              >
                <IconGlobe aria-hidden />
                <Select
                  value={locale}
                  options={LOCALES.map((l) => ({
                    value: l.code,
                    label: localeDisplayName(l.code),
                  }))}
                  onChange={(v) => {
                    setLocaleErrorDismissed(false);
                    setLocale(v as Locale);
                  }}
                  label={t("lang.label")}
                  placement="right"
                  portal={false}
                  style={{ flex: 1, minWidth: 0, width: "100%" }}
                />
              </div>
              {themeSwitch}
            </div>
          </div>
        </aside>

        <main className="main" inert={navOpen}>
          {quotaSummary}
          <div key={API_BASE}>
            <div className="main-inner">
              <div className="console-header-slot" ref={setHeaderTarget} />
              {/*
            Outside the page-keyed ErrorBoundary on purpose: that boundary remounts on
            every page change, which would destroy the button the user just activated
            and drop keyboard focus to <body>.
          */}
              {sectionPages.length > 1 && (
                <SectionSwitcher
                  items={sectionPages.map((member) => ({
                    page: member,
                    label: t(PAGE_TKEY[member]),
                  }))}
                  currentPage={page}
                  onNavigate={navigateToPage}
                  ariaLabel={t("nav.sectionNavigation")}
                  onFocusOrphaned={focusAfterSwitcher}
                />
              )}

              <PageHeaderTarget.Provider value={headerTarget}>
                <div className="console-page-body">
                  <ErrorBoundary
                    key={shellPage}
                    pageName={t(PAGE_TKEY[shellPage])}
                    title={t("errorBoundary.title")}
                    message={t("errorBoundary.message")}
                    detailsLabel={t("errorBoundary.details")}
                    reloadLabel={t("errorBoundary.reload")}
                  >
                    {capabilities.loading ? (
                      <div className="min-h-64" aria-busy="true" />
                    ) : !capabilities.data && capabilities.error ? (
                      <Notice tone="err">
                        {t("prov.loadConfigFail")}{" "}
                        <Button
                          onClick={() => capabilities.refresh()}
                          disabled={capabilities.refreshing}
                        >
                          {t("common.retry")}
                        </Button>
                      </Notice>
                    ) : !available ? (
                      <Notice tone="warn">{t("shellV2.pageUnavailable")}</Notice>
                    ) : (
                      <Suspense
                        key={page}
                        fallback={
                          <section data-page-loading aria-busy="true" className="min-h-64">
                            <PageHeader title={t(PAGE_TKEY[page])} />
                            <DataSurfaceSkeleton label={t("common.loading")} />
                          </section>
                        }
                      >
                        {page === "home" && (
                          <Home
                            apiBase={API_BASE}
                            navigate={navigateToPage}
                            onReloadCodex={() => {
                              void handleCodexRestart();
                            }}
                            reloadingCodex={codexRestarting}
                          />
                        )}
                        {page === "dashboard" && <Dashboard apiBase={API_BASE} />}
                        {page === "providers" && <Providers apiBase={API_BASE} />}
                        {page === "models" && (
                          <Models
                            key={API_BASE}
                            apiBase={API_BASE}
                            restartEpoch={codexRestartEpoch}
                            reportRestart={report}
                          />
                        )}
                        {page === "subagents" && <Subagents key={API_BASE} apiBase={API_BASE} />}

                        {page === "logs" && <Logs apiBase={API_BASE} />}
                        {page === "usage" && <Usage apiBase={API_BASE} />}
                        {page === "storage" && <LiteStorage apiBase={API_BASE} />}

                        {shellPage === "integrations" && <Integrations apiBase={API_BASE} />}
                      </Suspense>
                    )}
                  </ErrorBoundary>
                </div>
              </PageHeaderTarget.Provider>
            </div>
          </div>
        </main>
      </div>
    </RuntimeCapabilitiesContext.Provider>
  );
}
