/**
 * The page's toast host, reached from inside the island.
 *
 * templates/partials/_toast_host.html is included once by base.html and listens
 * for `showToast` on document.body. It renders title and body through
 * textContent only, so nothing here is an injection surface, and it is ordinary
 * bundled code rather than an inline script, so CSP does not come into it.
 *
 * Used for facts the user would otherwise have to infer from a header they were
 * not looking at. Failures stay in the ProblemsRail instead, next to the
 * problems they name -- a toast would be a second copy that dismisses itself
 * while pointing away from the list.
 */
export type ToastTone = "success" | "info" | "warn" | "error";

export function showToast(detail: { tone: ToastTone; title: string; body?: string }): void {
  if (typeof document === "undefined") {
    return;
  }
  document.body.dispatchEvent(new CustomEvent("showToast", { detail }));
}
