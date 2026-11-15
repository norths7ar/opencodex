/**
 * The sidebar's rows, in order.
 *
 * A row is a navigation group, not a page. Most rows hold one page; Usage & Logs,
 * Route each hold several existing pages, reached through the section switcher
 * App renders above the page. Grouping changes no route: every member keeps the hash it
 * always had, so bookmarks and deep links still open the same page, and the row that
 * owns that page lights up.
 *
 * The first member is the row's destination. Order is the contract: Usage opens first
 * inside Usage & Logs because it leads that list.
 *
 * `embedded` pages belong to a row without being switcher members: Claude lives as a tab
 * inside the Route page, keeps its own #claude/* hashes, and lights Route up.
 */
import type { Page } from "./app-routing";
import type { TKey } from "./i18n/shared";
import {
  IconActivity,
  IconBot,
  IconBoxes,
  IconCodex,
  IconGrid,
  IconList,
  IconPlug,
  IconRoute,
  IconServer,
} from "./icons";

export type NavGroupId =
  | "home"
  | "models"
  | "providers"
  | "accounts"
  | "subagents"
  | "usage-logs"
  | "connect"
  | "proxy"
  | "advanced";

export interface NavGroup {
  id: NavGroupId;
  tkey: TKey;
  Icon: typeof IconGrid;
  pages: readonly [Page, ...Page[]];
  embedded?: readonly Page[];
}

export const NAV_GROUPS: readonly NavGroup[] = [
  // Startup lives inside Home again: how the proxy starts is part of its at-a-glance health,
  // and Home's switcher reaches it in one click.
  {
    id: "home",
    tkey: "shellV2.navHome",
    Icon: IconGrid,
    pages: ["home", "startup"],
  },
  // Route: where each client's traffic goes (integrations, the setup checklist, Claude as a tab).
  // It sits above Models because choosing the route comes before choosing the model.
  {
    id: "connect",
    tkey: "nav.connect",
    Icon: IconRoute,
    pages: ["integrations", "setup"],
    embedded: ["claude"],
  },
  // Providers: every non-Codex upstream and its quota, on its own row right under Route,
  // since adding a provider is the step that gives a route somewhere to go.
  { id: "providers", tkey: "nav.providers", Icon: IconPlug, pages: ["providers"] },
  { id: "models", tkey: "nav.models", Icon: IconBoxes, pages: ["models"] },
  // Codex's account pool: which account is serving and how much it has left.
  {
    id: "accounts",
    tkey: "shellV2.navAccounts",
    Icon: IconCodex,
    pages: ["codex-set"],
  },
  // Which model a helper agent runs on is changed as often as the main model.
  { id: "subagents", tkey: "nav.subagents", Icon: IconBot, pages: ["subagents"] },
  {
    id: "usage-logs",
    tkey: "nav.usageLogs",
    Icon: IconActivity,
    pages: ["usage", "logs", "storage"],
  },
  // Runtime capabilities decide which of these destinations are offered.
  {
    id: "proxy",
    tkey: "shellV2.pageProxyDetails",
    Icon: IconServer,
    pages: ["dashboard"],
  },
  {
    id: "advanced",
    tkey: "shellV2.pageAllSettings",
    Icon: IconList,
    pages: ["advanced"],
  },
];

/** The row that owns a page, or null for a page outside the sidebar. */
export function groupForPage(page: Page): NavGroup | null {
  return (
    NAV_GROUPS.find((group) => group.pages.includes(page) || group.embedded?.includes(page)) ?? null
  );
}

export function visibleGroupPages(group: NavGroup): Page[] {
  return [...group.pages];
}
