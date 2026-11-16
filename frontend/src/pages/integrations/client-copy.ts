import { useI18n, type TKey } from "../../i18n/shared";

/* eslint-disable local-i18n/no-hardcoded-ui-strings -- Type-only translation key prefix. */
type Key = TKey extends infer K
  ? K extends `integrations.clientCopy.${infer Name}`
    ? Name
    : never
  : never;
/* eslint-enable local-i18n/no-hardcoded-ui-strings */

export function useClientCopy() {
  const { t } = useI18n();
  return (key: Key): string => t(`integrations.clientCopy.${key}`);
}
