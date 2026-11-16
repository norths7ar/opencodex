import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getLoadedCatalog, loadCatalog, type Catalog } from "./catalogs";
import { en } from "./en";
import {
  I18nContext,
  LOCALES,
  detectInitial,
  interpolate,
  setActiveLocale,
  useI18n,
  type Locale,
  type TFn,
  type TKey,
  type Vars,
} from "./shared";

interface Language {
  locale: Locale;
  catalog: Catalog;
  persist: boolean;
}

function initialLanguage(locale: Locale): Promise<Language> {
  return loadCatalog(locale).then(
    (catalog) => ({ locale, catalog, persist: true }),
    () => ({ locale: "en", catalog: en, persist: false }),
  );
}

export function LanguageProvider({
  children,
  fallback = null,
}: {
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const [initialLocale] = useState(detectInitial);
  const [language, setLanguage] = useState<Language | null>(() => {
    const catalog = getLoadedCatalog(initialLocale);
    return catalog ? { locale: initialLocale, catalog, persist: true } : null;
  });
  const [switchStatus, setSwitchStatus] = useState<"idle" | "loading" | "error">("idle");
  const requestId = useRef(0);

  useEffect(() => {
    // The initial catalog may have finished between the first render and this effect.
    let cancelled = false;
    if (language === null) {
      void initialLanguage(initialLocale).then((next) => {
        if (!cancelled) {
          setLanguage(next);
          if (!next.persist) setSwitchStatus("error");
        }
      });
    }
    return () => {
      cancelled = true;
    };
  }, [initialLocale, language]);

  useEffect(
    () => () => {
      requestId.current++;
    },
    [],
  );

  const setLocale = useCallback((next: Locale) => {
    const id = ++requestId.current;
    const commit = (catalog: Catalog) => {
      if (id === requestId.current) {
        setActiveLocale(next);
        setLanguage({ locale: next, catalog, persist: true });
        setSwitchStatus("idle");
      }
    };
    const cached = getLoadedCatalog(next);
    if (cached) commit(cached);
    else {
      setSwitchStatus("loading");
      void loadCatalog(next).then(commit, () => {
        if (id === requestId.current) setSwitchStatus("error");
      });
    }
  }, []);

  useEffect(() => {
    if (!language) return;
    const { locale, persist } = language;
    setActiveLocale(locale);
    const meta = LOCALES.find((l) => l.code === locale) ?? LOCALES[0];
    document.documentElement.lang = meta.htmlLang;
    if (!persist) return;
    try {
      localStorage.setItem("ocx-lang", locale);
    } catch {
      /* ignore */
    }
  }, [language]);

  const locale = language?.locale ?? initialLocale;
  const catalog = language?.catalog ?? en;
  const t: TFn = useCallback(
    (key, vars) => interpolate(catalog[key] ?? en[key] ?? key, vars),
    [catalog],
  );
  const localeLoading = language === null || switchStatus === "loading";
  const localeError = switchStatus === "error";
  const value = useMemo(
    () => ({ locale, localeLoading, localeError, setLocale, t }),
    [locale, localeLoading, localeError, setLocale, t],
  );

  return (
    <I18nContext.Provider value={value}>{language ? children : fallback}</I18nContext.Provider>
  );
}

export function Trans({ k, cmd, vars }: { k: TKey; cmd: string; vars?: Vars }) {
  const { t } = useI18n();
  const [pre, post = ""] = t(k, vars).split("{cmd}");
  return (
    <>
      {pre}
      <code className="chip">{cmd}</code>
      {post}
    </>
  );
}
