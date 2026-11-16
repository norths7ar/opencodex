import { useEffect, useId, useRef, type ReactNode } from "react";
import { Button } from "../../components/button";
import { useClientCopy } from "./client-copy";

export default function ClientDialog({
  title,
  children,
  pending,
  disabled,
  onConfirm,
  onClose,
}: {
  title: string;
  children: ReactNode;
  pending: boolean;
  disabled?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const copy = useClientCopy();
  const ref = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    const previous = document.activeElement;
    dialog?.showModal();
    cancel.current?.focus();
    return () => {
      dialog?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className="client-dialog"
      aria-labelledby={titleId}
      aria-busy={pending}
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onClose();
      }}
    >
      <h3 id={titleId}>{title}</h3>
      <div className="client-dialog-body">{children}</div>
      <footer className="client-actions">
        <Button ref={cancel} disabled={pending} onClick={onClose}>
          {copy("cancel")}
        </Button>
        <Button variant="primary" disabled={pending || disabled} onClick={onConfirm}>
          {copy(pending ? "working" : "confirm")}
        </Button>
      </footer>
    </dialog>
  );
}
