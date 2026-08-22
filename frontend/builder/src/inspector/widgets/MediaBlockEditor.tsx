/**
 * A `block_media` — image, audio, video or file, from the library or a URL.
 *
 * The schema requires `media_id` **or** `url` (an `anyOf`), and this widget owns
 * that choice so the two can never both be set: picking from the library clears
 * the URL and vice versa. Leaving both would be a config the server rejects with
 * no obvious cause.
 */
import { useState } from "react";

import type { MediaAsset } from "../../schema/types";
import { formatPath } from "../../store/paths";
import { useField } from "../FieldContext";
import { SchemaField } from "../SchemaField";
import type { FieldProps } from "../fields";
import { MediaPickerDialog } from "./MediaPickerDialog";

export function MediaBlockEditor(props: FieldProps) {
  const { set, clear, readOnly, env, picklists } = useField();
  const { schema, path, value } = props;
  const block = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  const [picking, setPicking] = useState(false);

  const mediaId = typeof block["media_id"] === "string" ? block["media_id"] : "";
  const url = typeof block["url"] === "string" ? block["url"] : "";

  // The first connected channel, so the picker can annotate assets that would
  // be too large for it. Advisory: the target platform is not fixed until send.
  const platform = picklists.connections[0]?.platform ?? "";

  const choose = (asset: MediaAsset) => {
    set([...path, "media_id"], asset.id, `media:${formatPath(path)}`);
    clear([...path, "url"]);
    setPicking(false);
  };

  return (
    <div className="fb-field">
      {mediaId ? (
        <p className="fb-field-help flex items-center gap-2">
          <span className="fb-pill">Library asset {mediaId.slice(0, 8)}…</span>
          {readOnly ? null : (
            <button
              type="button"
              className="btn-link text-xs"
              aria-label="Remove the chosen library asset"
              onClick={() => clear([...path, "media_id"])}
            >
              Remove
            </button>
          )}
        </p>
      ) : null}

      {readOnly ? null : (
        <button type="button" className="btn-outline-sm mb-2" onClick={() => setPicking((open) => !open)}>
          {picking ? "Close the library" : "Choose from the library"}
        </button>
      )}

      {picking ? (
        <MediaPickerDialog
          env={env}
          platform={platform}
          kind={String(block["type"] ?? "")}
          onPick={choose}
          onClose={() => setPicking(false)}
        />
      ) : null}

      <div className="fb-field">
        <label className="fb-field-label" htmlFor={`fb-url-${path.join("-")}`}>
          …or a direct URL
        </label>
        <input
          id={`fb-url-${path.join("-")}`}
          type="url"
          className="form-input-styled"
          value={url}
          disabled={readOnly || Boolean(mediaId)}
          placeholder={mediaId ? "Using a library asset" : "https://example.com/image.png"}
          onChange={(event) => {
            const next = event.target.value;
            if (next === "") {
              clear([...path, "url"]);
              return;
            }
            set([...path, "url"], next, `url:${formatPath(path)}`);
            clear([...path, "media_id"]);
          }}
        />
        <p className="fb-field-help">
          A library asset survives a storage change; a URL is yours to keep working.
        </p>
      </div>

      <SchemaField
        schema={schema.properties?.["caption"]}
        path={[...path, "caption"]}
        value={block["caption"]}
        propertyName="caption"
      />
    </div>
  );
}
