import { useState } from "react";

/** Remembered open/closed state for a collapsible section.
 *
 * Open the first time (so newcomers see the guidance), then remembers the
 * user's choice in localStorage — so returning users get a clean, uncluttered
 * page. `storageKey` namespaces the memory per section.
 */
export function useCollapse(storageKey: string, defaultOpen = true): [boolean, () => void] {
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(storageKey);
      return v === null ? defaultOpen : v === "1";
    } catch {
      return defaultOpen;
    }
  });
  const toggle = () => {
    setOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(storageKey, next ? "1" : "0");
      } catch {
        /* localStorage unavailable (private mode) — just don't remember */
      }
      return next;
    });
  };
  return [open, toggle];
}
