import { en, type TKey } from "./en";
import { de } from "./de";
import { fr } from "./fr";
import { ko } from "./ko";
import { zh } from "./zh";
import { zhTW } from "./zh-TW";
import { ru } from "./ru";
import { ja } from "./ja";
import { tr } from "./tr";
import { vi } from "./vi";
import { pt } from "./pt";

/** React-free locale catalog registry for formatters and other shared helpers. */
export type Locale = "en" | "de" | "fr" | "ko" | "zh" | "zh-TW" | "ru" | "ja" | "tr" | "vi" | "pt";

export const DICTS: Record<Locale, Record<TKey, string>> = {
  en: en,
  de: de,
  fr: fr,
  ko: ko,
  zh: zh,
  "zh-TW": zhTW,
  ru: ru,
  ja: ja,
  tr: tr,
  vi: vi,
  pt: pt,
};

/** Native language names shown by the language picker, kept inside i18n rather than UI metadata. */
export function localeDisplayName(locale: Locale): string {
  return DICTS[locale]["lang.nativeName"];
}

export function catalogValue(locale: Locale, key: TKey): string {
  return DICTS[locale][key];
}

export type { TKey };
