/**
 * `send_message.buttons` and `.quick_replies`.
 *
 * The generic array field already handles add / remove / reorder from the
 * schema. What this adds is the two things it cannot know: minting an `id` for
 * a new item — which is what makes the `btn:<id>` handle appear on the card the
 * moment the button exists — and naming the channels the limits will be checked
 * against.
 *
 * It deliberately does NOT carry a capability table. apps/flows/capabilities.py
 * owns that, the server emits `capability_limit_exceeded` and
 * `capability_unsupported` against `config.buttons`, and those render inline
 * through the ordinary issue path. A second copy here would be the vendored
 * duplicate the brief forbids, and would go stale the day a platform changes.
 */
import { newItemId } from "../../schema/ids";
import { formatPath } from "../../store/paths";
import { useField } from "../FieldContext";
import { ArrayField, defaultFor } from "../SchemaField";
import type { FieldProps } from "../fields";

function ChannelHint() {
  const { picklists } = useField();
  if (picklists.connections.length === 0) {
    return <p className="fb-field-help">Limits depend on the channel this flow runs on; none are connected yet.</p>;
  }
  const names = [...new Set(picklists.connections.map((connection) => connection.platform))].join(", ");
  return <p className="fb-field-help">Checked against the connected channels: {names}.</p>;
}

export function idMintingList(propertyName: string) {
  return function IdMintingList(props: FieldProps) {
    const { set, readOnly } = useField();
    const { schema, path, value } = props;
    const items = Array.isArray(value) ? value : [];
    const max = schema.maxItems;

    return (
      <>
        <ArrayField {...props} />
        <ChannelHint />
        {readOnly || (max !== undefined && items.length >= max) ? null : (
          <button
            type="button"
            className="btn-outline-sm"
            onClick={() => {
              const seeded = defaultFor(schema.items);
              // The id is what the `btn:`/`qr:` handle is built from, so it is
              // minted here rather than typed — and it has to be unique within
              // the node or two handles collide.
              const item = typeof seeded === "object" && seeded !== null ? { ...seeded, id: newItemId() } : seeded;
              set(path, [...items, item], `append:${formatPath(path)}`);
            }}
          >
            Add a {propertyName}
          </button>
        )}
      </>
    );
  };
}
