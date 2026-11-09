/**
 * Keyboard shortcuts for the sidebar (components/sidebar-nav.tsx): which modifier the
 * number shortcuts use on this surface, and how a shortcut is written for people.
 */

export function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement
    && (target.isContentEditable || target.closest("input, textarea, select, [contenteditable]") !== null);
}

export type ShortcutStyle = "browser-mac" | "browser";

/** Browsers reserve Cmd/Ctrl+digit; dashboard navigation uses Option/Alt. */
export function shortcutStyle(userAgent = typeof navigator === "undefined" ? "" : navigator.userAgent): ShortcutStyle {
  const mac = /Mac OS X|Macintosh/i.test(userAgent);
  return mac ? "browser-mac" : "browser";
}

export function shortcutLabel(style: ShortcutStyle, key: string): string {
  switch (style) {
    case "browser-mac": return `⌥${key}`;
    default: return `Alt+${key}`;
  }
}

/** Menu search: Cmd+K on a Mac, Ctrl+K elsewhere, in the shell and the browser alike. */
export function searchShortcutLabel(style: ShortcutStyle): string {
  return style === "browser-mac" ? "⌘K" : "Ctrl+K";
}

export function numberModifierHeld(event: KeyboardEvent): boolean {
  return event.altKey && !event.metaKey && !event.ctrlKey && !event.shiftKey;
}

