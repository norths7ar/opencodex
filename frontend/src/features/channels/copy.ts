import { useI18n } from "@/i18n/shared";
import type { TKey } from "@/i18n/shared";

type ChannelKey = TKey extends infer K ? (K extends `channels.${infer Key}` ? Key : never) : never;

export function useChannelCopy() {
  const { t } = useI18n();
  return (key: ChannelKey) => t(`channels.${key}`);
}
