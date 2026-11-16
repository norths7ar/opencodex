import { en, type TKey } from "./en";

const nativeNames = {
  en: "English",
  de: "Deutsch",
  fr: "Français",
  ko: "한국어",
  zh: "中文",
  "zh-TW": "繁體中文",
  ru: "Русский",
  ja: "日本語",
  tr: "Türkçe",
  vi: "Tiếng Việt",
  pt: "Português",
};

export type Locale = keyof typeof nativeNames;
export type Catalog = Readonly<Record<TKey, string>>;
export type { TKey };

export function localeDisplayName(locale: Locale): string {
  return nativeNames[locale];
}

const loaders: Record<Exclude<Locale, "en">, () => Promise<Partial<Catalog>>> = {
  de: () => import("./de").then(({ de }) => de),
  fr: () => import("./fr").then(({ fr }) => fr),
  ko: () => import("./ko").then(({ ko }) => ko),
  zh: () => import("./zh").then(({ zh }) => zh),
  "zh-TW": () => import("./zh-TW").then(({ zhTW }) => zhTW),
  ru: () => import("./ru").then(({ ru }) => ru),
  ja: () => import("./ja").then(({ ja }) => ja),
  tr: () => import("./tr").then(({ tr }) => tr),
  vi: () => import("./vi").then(({ vi }) => vi),
  pt: () => import("./pt").then(({ pt }) => pt),
};

const loaded = new Map<Locale, Catalog>([["en", en]]);
const pending = new Map<Locale, Promise<Catalog>>();

export function getLoadedCatalog(locale: Locale): Catalog | undefined {
  return loaded.get(locale);
}

export function loadCatalog(locale: Locale): Promise<Catalog> {
  if (locale === "en") return Promise.resolve(en);
  const cached = loaded.get(locale);
  if (cached) return Promise.resolve(cached);
  const loading = pending.get(locale);
  if (loading) return loading;

  const request = loaders[locale]()
    .then((translations) => {
      const catalog: Catalog = { ...en, ...translations };
      loaded.set(locale, catalog);
      return catalog;
    })
    .finally(() => pending.delete(locale));
  pending.set(locale, request);
  return request;
}
