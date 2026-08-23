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
  statsUrl: string;
  schemaUrl: string;
  mediaPickerUrl: string;
  /** SPEC §16's preview-link endpoint. */
  previewUrl: string;
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
    statsUrl: required(mount, "statsUrl"),
    schemaUrl: required(mount, "schemaUrl"),
    mediaPickerUrl: required(mount, "mediaPickerUrl"),
    previewUrl: required(mount, "previewUrl"),
  };
}
