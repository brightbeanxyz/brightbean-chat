/**
 * Which handles a node exposes — a port of
 * :func:`apps.flows.schema.nodes.handles_for_node`.
 *
 * The server derives the same set from the same config on every save, and an
 * edge naming a handle it does not derive is rejected as `handle_not_available`.
 * So a divergence here does not degrade gracefully: it breaks *every* save of
 * any graph containing the affected node. Keep this function and its Python
 * original in step, and note that the "id must be a string" clause below is
 * part of that contract, not a defensive nicety.
 */
import { nodeSpec } from "./artifact";
import type { NodeTypeSpec } from "./types";

/** apps/flows/schema/handles.py::HANDLE_PATTERN, verbatim. */
export const HANDLE_PATTERN = /^(?:default|timeout|error|cond:(?:true|false)|(?:btn|qr|rand):[A-Za-z0-9_-]{1,64})$/;

/** apps/flows/schema/envelope.py::ID_PATTERN, verbatim. */
export const ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Every handle this node actually exposes, static and config-derived, in the
 * order the canvas should draw them down the card's right edge.
 *
 * Static handles keep their registry order; derived ones follow in config
 * order, so a button dragged to the top of the list moves its handle with it.
 */
export function sourceHandles(spec: NodeTypeSpec, config: unknown): string[] {
  const handles = [...spec.handles];
  if (!isRecord(config)) {
    return handles;
  }
  for (const { prefix, config_key } of spec.dynamic_handles) {
    const items = config[config_key];
    if (!Array.isArray(items)) {
      continue;
    }
    for (const item of items) {
      // A non-string id contributes no handle server-side. Accepting one here
      // would draw a handle the validator refuses to route through.
      if (isRecord(item) && typeof item["id"] === "string") {
        handles.push(`${prefix}:${item["id"]}`);
      }
    }
  }
  return handles;
}

/** The same, looked up by node type. Unknown types expose nothing. */
export function sourceHandlesFor(type: string, config: unknown): string[] {
  const spec = nodeSpec(type);
  return spec ? sourceHandles(spec, config) : [];
}

/** Human copy for a handle, given the source node's config for the dynamic ones. */
export function handleLabel(handle: string, config: unknown): string {
  switch (handle) {
    case "default":
      return "";
    case "timeout":
      return "Timeout";
    case "error":
      return "Error";
    case "cond:true":
      return "Yes";
    case "cond:false":
      return "No";
  }

  const separator = handle.indexOf(":");
  if (separator === -1) {
    return handle;
  }
  const prefix = handle.slice(0, separator);
  const id = handle.slice(separator + 1);

  const key = { btn: "buttons", qr: "quick_replies", rand: "paths" }[prefix];
  if (key && isRecord(config) && Array.isArray(config[key])) {
    const item = (config[key] as unknown[]).find((entry) => isRecord(entry) && entry["id"] === id);
    if (isRecord(item)) {
      if (typeof item["label"] === "string" && item["label"]) {
        return item["label"];
      }
      if (typeof item["weight"] === "number") {
        return `${item["weight"]}%`;
      }
    }
  }
  return id;
}
