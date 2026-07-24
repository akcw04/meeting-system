import { useEffect, useRef } from "react";

/** In-app confirmation dialog — replaces window.confirm, whose native popup
 * is branded by the browser ("localhost:5173 says…") and cannot be styled.
 * A dimmed overlay with a small card: Escape, the overlay, or Cancel dismisses;
 * the destructive action is the filled red button. Focus starts on Cancel so
 * Enter never destroys anything by accident. */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Delete",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">{title}</div>
        <div className="modal-msg">{message}</div>
        <div className="modal-actions">
          <button ref={cancelRef} className="ghost small" onClick={onCancel}>
            Cancel
          </button>
          <button className="small modal-danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
