import { useI18n, type TKey } from "../../i18n/shared";

type Key = TKey extends infer K
  ? K extends `integrations.clientCopy.${infer Name}`
    ? Name
    : never
  : never;

export function useClientCopy() {
  const { t } = useI18n();
  return (key: Key): string => t(`integrations.clientCopy.${key}`);
}
