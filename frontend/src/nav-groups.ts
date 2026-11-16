/** Seven business destinations; Usage owns its logs and storage subpages. */
import type { Page } from "./app-routing";
import type { TKey } from "./i18n/shared";
import {
  IconActivity,
  IconBot,
  IconBoxes,
  IconGrid,
  IconPlug,
  IconRoute,
  IconServer,
} from "./icons";

export type NavGroupId =
  "home" | "models" | "providers" | "subagents" | "usage-logs" | "connect" | "proxy";

export interface NavGroup {
  id: NavGroupId;
  tkey: TKey;
  Icon: typeof IconGrid;
  pages: readonly [Page, ...Page[]];
  embedded?: readonly Page[];
}

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    id: "home",
    tkey: "shellV2.navHome",
    Icon: IconGrid,
    pages: ["home"],
  },
  {
    id: "connect",
    tkey: "nav.connect",
    Icon: IconRoute,
    pages: ["integrations"],
  },
  { id: "providers", tkey: "nav.providers", Icon: IconPlug, pages: ["providers"] },
  { id: "models", tkey: "nav.models", Icon: IconBoxes, pages: ["models"] },
  { id: "subagents", tkey: "nav.subagents", Icon: IconBot, pages: ["subagents"] },
  {
    id: "usage-logs",
    tkey: "nav.usageLogs",
    Icon: IconActivity,
    pages: ["usage", "logs", "storage"],
  },
  {
    id: "proxy",
    tkey: "shellV2.pageProxyDetails",
    Icon: IconServer,
    pages: ["dashboard"],
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
