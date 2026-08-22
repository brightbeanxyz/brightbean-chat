import { describe, expect, it } from "vitest";

import { MissingEnvError, readEnv } from "./env";

function mount(dataset: Record<string, string>): HTMLElement {
  const element = document.createElement("div");
  for (const [key, value] of Object.entries(dataset)) {
    element.setAttribute(`data-${key}`, value);
  }
  return element;
}

const COMPLETE = {
  "flow-id": "flow-1",
  "can-edit": "true",
  "detail-url": "/w/ws/api/flows/flow-1/",
  "publish-url": "/w/ws/api/flows/flow-1/publish/",
  "stats-url": "/w/ws/api/flows/flow-1/stats/",
  "schema-url": "/w/ws/api/flows/schema/",
  "media-picker-url": "/w/ws/media/picker/",
  "preview-url": "/w/ws/settings/channels/telegram/preview/flow-1/",
};

describe("reading the mount div", () => {
  it("parses every URL the view hands over", () => {
    expect(readEnv(mount(COMPLETE))).toEqual({
      flowId: "flow-1",
      canEdit: true,
      detailUrl: "/w/ws/api/flows/flow-1/",
      publishUrl: "/w/ws/api/flows/flow-1/publish/",
      statsUrl: "/w/ws/api/flows/flow-1/stats/",
      schemaUrl: "/w/ws/api/flows/schema/",
      mediaPickerUrl: "/w/ws/media/picker/",
      previewUrl: "/w/ws/settings/channels/telegram/preview/flow-1/",
    });
  });

  it('treats data-can-edit="false" as false', () => {
    // Django's `yesno` writes the literal string "false", which is truthy in
    // JavaScript. Getting this wrong hands a Viewer a fully editable canvas
    // whose every save is a 403 — the single most likely bug in this feature.
    expect(readEnv(mount({ ...COMPLETE, "can-edit": "false" })).canEdit).toBe(false);
  });

  it.each(["", "False", "0", "no", "yes", "1"])("treats data-can-edit=%s as false too", (value) => {
    expect(readEnv(mount({ ...COMPLETE, "can-edit": value })).canEdit).toBe(false);
  });

  it("refuses a mount div missing a URL rather than fetching undefined", () => {
    const { "detail-url": _omitted, ...incomplete } = COMPLETE;

    expect(() => readEnv(mount(incomplete))).toThrow(MissingEnvError);
  });
});
