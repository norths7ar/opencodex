import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { computeSelectMenuStyle } from "../select-position";
import "./help-popover.css";

export function HelpSurface({
  id,
  label,
  anchor,
  onClose,
  children,
}: {
  id: string;
  label: string;
  anchor: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  children: ReactNode;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const focused = useRef(false);
  const [position, setPosition] = useState<CSSProperties>({ visibility: "hidden" });
  useLayoutEffect(() => {
    const node = surface.current;
    if (!node) return;
    node.showPopover?.();
    const update = () => {
      if (!anchor.current) return;
      setPosition(
        computeSelectMenuStyle(anchor.current.getBoundingClientRect(), {
          menuWidth: 380,
          menuHeight: node.scrollHeight,
          maxMenuHeight: Math.min(480, window.innerHeight - 24),
        }),
      );
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [anchor]);
  useLayoutEffect(() => {
    if (position.visibility === "hidden" || focused.current) return;
    surface.current?.focus({ preventScroll: true });
    focused.current = true;
  }, [position]);
  useEffect(() => {
    const outside = (event: Event) => {
      const node = event.target as Node;
      if (!surface.current?.contains(node) && !anchor.current?.contains(node)) onClose();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
      anchor.current?.focus({ preventScroll: true });
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", key);
    };
  }, [anchor, onClose]);
  return createPortal(
    <div
      ref={surface}
      id={id}
      popover="manual"
      role="dialog"
      tabIndex={-1}
      aria-label={label}
      aria-describedby={`${id}-content`}
      className="ocx-help-surface"
      style={position}
    >
      <div id={`${id}-content`}>{children}</div>
    </div>,
    document.body,
  );
}
