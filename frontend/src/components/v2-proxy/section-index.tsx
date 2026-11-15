import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "motion/react";
import { cn } from "@/lib/utils";

export interface SectionIndexItem {
  id: string;
  label: string;
}

/*
 * In-page index for a long settings page (React Bits Pro settings-form-1 navigation): a
 * sticky column of section names on wide screens, a scrolling strip on narrow ones. It only
 * scrolls; the URL hash stays the page route, so Back still leaves the page. The section in
 * view is marked with aria-current and the inverse-neutral selected fill.
 */
export function SectionIndex({
  items,
  label,
  className,
}: {
  items: readonly SectionIndexItem[];
  label: string;
  className?: string;
}) {
  const reduce = useReducedMotion();
  const [active, setActive] = useState(items[0]?.id ?? "");
  const destination = useRef<string | null>(null);
  const ids = items.map((item) => item.id).join("|");

  useEffect(() => {
    const sections = ids.split("|").flatMap((id) => {
      const node = document.getElementById(id);
      return node ? [node] : [];
    });
    let frame = 0;
    const update = () => {
      frame = 0;
      // A selected destination remains selected throughout the animated scroll,
      // including short sections near the document bottom that cannot reach the top.
      if (destination.current || !sections.length) return;
      const offset = Number.parseFloat(getComputedStyle(sections[0]).scrollMarginTop) || 0;
      const passed = sections.filter((node) => node.getBoundingClientRect().top <= offset + 1);
      const atBottom =
        window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 1;
      setActive((atBottom ? sections.at(-1) : (passed.at(-1) ?? sections[0]))!.id);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    const release = () => {
      destination.current = null;
    };
    const onKey = (event: KeyboardEvent) => {
      if (["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End", " "].includes(event.key))
        release();
    };
    // User scrolling takes over immediately; pointerdown also covers dragging the scrollbar.
    window.addEventListener("wheel", release, { passive: true });
    window.addEventListener("touchstart", release, { passive: true });
    window.addEventListener("pointerdown", release);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    update();
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("wheel", release);
      window.removeEventListener("touchstart", release);
      window.removeEventListener("pointerdown", release);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      destination.current = null;
    };
  }, [ids]);

  const go = (id: string) => {
    const node = document.getElementById(id);
    if (!node) return;
    destination.current = id;
    setActive(id);
    node.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    // Move focus with the view so keyboard users continue from the section they chose.
    const heading = node.querySelector<HTMLElement>("h3, h2");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  };

  return (
    <nav aria-label={label} className={cn("pv2-index min-w-0", className)}>
      <p className="m-0 mb-1.5 hidden px-2.5 text-xs font-medium text-fg-subtle lg:block">
        {label}
      </p>
      <ul className="m-0 flex list-none gap-1 overflow-x-auto p-0 pb-1 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:pb-0">
        {items.map((item) => {
          const current = item.id === active;
          return (
            <li key={item.id} className="shrink-0">
              <button
                type="button"
                aria-current={current ? "location" : undefined}
                onClick={() => go(item.id)}
                className={cn(
                  "flex h-[var(--ocx-control-h-sm)] w-full cursor-pointer items-center rounded-[8px] border-0 bg-transparent px-2.5 text-left text-[13px] font-medium whitespace-nowrap text-fg-muted",
                  "transition-colors duration-150 hover:bg-surface hover:text-fg",
                  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-highlight",
                  current && "bg-surface text-fg shadow-[inset_0_0_0_1px_var(--ocx-line)]",
                )}
              >
                {item.label}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
