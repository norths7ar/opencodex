/** Dashboard visibility follows the browser tab, including hidden-window tabs. */
export function hostDocumentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function onHostVisibilityChange(callback: () => void): () => void {
  let last = hostDocumentHidden();
  const notify = () => {
    const next = hostDocumentHidden();
    if (next === last) return;
    last = next;
    callback();
  };
  if (typeof document !== "undefined") document.addEventListener("visibilitychange", notify);
  return () => {
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", notify);
  };
}
