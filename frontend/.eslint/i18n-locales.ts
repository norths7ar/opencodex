import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const I18N_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src", "i18n");

/** Source locale / `TKey` definition (`src/i18n/en.ts`). */
export const I18N_SOURCE_LOCALE = "en";

/** Locale module ids matching `src/i18n/{id}.ts` (discovered at lint time). */
function listI18nLocaleModules(): string[] {
  if (!fs.existsSync(I18N_DIR)) return [I18N_SOURCE_LOCALE];
  return fs
    .readdirSync(I18N_DIR)
    .filter((name) => name.endsWith(".ts"))
    .map((name) => name.replace(/\.ts$/, ""))
    .filter((id) => /^[a-z]{2}(?:-[A-Z]{2})?$/.test(id))
    .sort((a, b) => {
      if (a === I18N_SOURCE_LOCALE) return -1;
      if (b === I18N_SOURCE_LOCALE) return 1;
      return a.localeCompare(b);
    });
}

/** Available dictionaries; missing translations fall back to the source entry. */
export function i18nLocaleFileHint(): string {
  const modules = listI18nLocaleModules();
  if (modules.length <= 1) {
    return `src/i18n/${I18N_SOURCE_LOCALE}.ts`;
  }
  const files = modules.map((code) => `src/i18n/${code}.ts`);
  return files.join(", ");
}

export function formatHardcodedSnippet(value: string, maxLen = 56): string {
  const oneLine = value.replace(/\s+/g, " ").trim();
  if (!oneLine) return "(empty)";
  if (oneLine.length <= maxLen) return oneLine;
  return `${oneLine.slice(0, maxLen - 1)}…`;
}
