/** File integration tabs supported by Lite, plus their overview. */
import type { TKey } from "../../i18n/shared";

export type IntegrationTab = "overview" | "codex" | "dsh";

export interface TabDefinition {
  id: IntegrationTab;
  hash: string;
  labelKey: TKey;
}

export const TABS: readonly TabDefinition[] = [
  { id: "overview", hash: "integrations", labelKey: "integrations.tab.overview" },
  { id: "codex", hash: "integrations/codex", labelKey: "integrations.tab.codex" },
  { id: "dsh", hash: "integrations/dsh", labelKey: "integrations.tab.dsh" },
];
