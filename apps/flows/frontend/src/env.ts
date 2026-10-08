/**
 * The mount div's data attributes, typed.
 *
 * apps/flows/views.py reverses every URL here, so the island assembles none of
 * its own — `location.pathname` arithmetic would break the moment the app is
 * deployed under FORCE_SCRIPT_NAME.
 */

export interface BuilderEnv {
  flowId: string;
  canEdit: boolean;
  detailUrl: string;
  publishUrl: string;
  /** Takes a live flow offline (apps/flows/api.py's flow_take_offline). */
  offlineUrl: string;
  statsUrl: string;
  schemaUrl: string;
  mediaPickerUrl: string;
  /** SPEC §16's preview-link endpoint. */
  previewUrl: string;
  /**
   * The top bar's (HANDOFF §3, Flow builder). Optional, unlike the API URLs
   * above: a page that omits one loses that control — the back link, the
   * rename, a download, a trigger switch — not the canvas.
   */
  flowName: string;
  listUrl: string;
  renameUrl: string;
  /** Holds TRIGGER_ID_PLACEHOLDER where the trigger's id goes; see triggerEnabledUrl(). */
  triggerEnabledUrl: string;
  exportUrl: string;
  exportBundleUrl: string;
}

/** apps/flows/views.py's TRIGGER_ID_PLACEHOLDER: the nil UUID, which no row has. */
export const TRIGGER_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000";

/** The switch endpoint for one trigger, or "" when the page did not provide it. */
export function triggerEnabledUrl(env: BuilderEnv, triggerId: string): string {
  return env.triggerEnabledUrl ? env.triggerEnabledUrl.replace(TRIGGER_ID_PLACEHOLDER, triggerId) : "";
}

export class MissingEnvError extends Error {}

function required(mount: HTMLElement, key: keyof DOMStringMap): string {
  const value = mount.dataset[key];
  if (!value) {
    throw new MissingEnvError(`The flow-builder mount div is missing data-${String(key)}.`);
  }
  return value;
}

export function readEnv(mount: HTMLElement): BuilderEnv {
  return {
    flowId: required(mount, "flowId"),
    // Django's `yesno` writes the literal string "false", which is truthy in
    // JavaScript. Comparing against "true" is the only safe reading, and
    // getting it wrong hands a Viewer a fully editable canvas whose every save
    // is a 403.
    canEdit: mount.dataset["canEdit"] === "true",
    detailUrl: required(mount, "detailUrl"),
    publishUrl: required(mount, "publishUrl"),
    offlineUrl: required(mount, "offlineUrl"),
    statsUrl: required(mount, "statsUrl"),
    schemaUrl: required(mount, "schemaUrl"),
    mediaPickerUrl: required(mount, "mediaPickerUrl"),
    previewUrl: required(mount, "previewUrl"),
    flowName: mount.dataset["flowName"] ?? "",
    listUrl: mount.dataset["listUrl"] ?? "",
    renameUrl: mount.dataset["renameUrl"] ?? "",
    triggerEnabledUrl: mount.dataset["triggerEnabledUrl"] ?? "",
    exportUrl: mount.dataset["exportUrl"] ?? "",
    exportBundleUrl: mount.dataset["exportBundleUrl"] ?? "",
  };
}
