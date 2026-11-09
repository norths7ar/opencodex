/**
 * The sidebar's rows, in order.
 *
 * A row is a navigation group, not a page. Most rows hold one page; Subagents, Usage & Logs
 * and Route each hold several existing pages, reached through the section switcher
 * App renders above the page. Grouping changes no route: every member keeps the hash it
 * always had, so bookmarks and deep links still open the same page, and the row that
 * owns that page lights up.
 *
 * The first member is the row's destination. Order is the contract: Usage opens first
 * inside Usage & Logs because it leads that list.
 *
 * `embedded` pages belong to a row without being switcher members: Claude lives as a tab
 * inside the Connect page, keeps its own #claude/* hashes, and lights Connect up.
 */
import type { Page } from "./app-routing";
import type { TKey } from "./i18n/shared";
import { IconActivity, IconBot, IconBoxes, IconCodex, IconGrid, IconLink, IconServer } from "./icons";

export type NavGroupId =
  | "dashboard"
  | "connect"
  | "codex-set"
  | "providers"
  | "models"
  | "subagents"
  | "usage-logs"

export interface NavGroup {
  id: NavGroupId;
  tkey: TKey;
  Icon: typeof IconGrid;
  pages: readonly [Page, ...Page[]];
  embedded?: readonly Page[];
}

export const NAV_GROUPS: readonly NavGroup[] = [
  { id: "dashboard", tkey: "nav.dashboard", Icon: IconGrid, pages: ["dashboard"] },
  { id: "connect", tkey: "nav.connect", Icon: IconLink, pages: ["integrations"], embedded: ["claude"] },
  { id: "codex-set", tkey: "nav.codexSet", Icon: IconCodex, pages: ["codex-set"] },
  { id: "providers", tkey: "nav.providers", Icon: IconServer, pages: ["providers"] },
  { id: "models", tkey: "nav.models", Icon: IconBoxes, pages: ["models"] },
  // Advisor is another way a worker reaches a second model, so it sits beside Subagents
  // in the switcher instead of taking a sidebar row of its own.
  { id: "subagents", tkey: "nav.subagents", Icon: IconBot, pages: ["subagents"] },
  { id: "usage-logs", tkey: "nav.usageLogs", Icon: IconActivity, pages: ["usage", "logs", "storage"] },
];

/** The row that owns a page, or null for pages outside the sidebar (Startup). */
export function groupForPage(page: Page): NavGroup | null {
  return NAV_GROUPS.find(group => group.pages.includes(page) || group.embedded?.includes(page)) ?? null;
}

export function visibleGroupPages(group: NavGroup): Page[] {
  return [...group.pages];
}
