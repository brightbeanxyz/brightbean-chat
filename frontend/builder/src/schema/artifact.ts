/**
 * The node registry, as data.
 *
 * Everything the canvas knows about node types — which exist, what they are
 * called, which drawer they sit in, which handles they expose, what their
 * config may contain — comes from here, so a node type registered by a later
 * layer needs no bespoke canvas code. There is deliberately no hard-coded node
 * list anywhere in this bundle.
 */
import document from "@flow-schema";

import type { FlowSchemaDocument, GraphLimits, JsonSchema, NodeTypeSpec, PaletteGroup } from "./types";

export const SCHEMA: FlowSchemaDocument = document;

export const SCHEMA_VERSION: number = SCHEMA["x-brightbean"].schema_version;

export const LIMITS: GraphLimits = SCHEMA["x-brightbean"].limits;

export const NODE_TYPES: readonly NodeTypeSpec[] = SCHEMA["x-brightbean"].node_types;

/** The drawer a node type falls into when the document predates issue #10. */
export const FALLBACK_GROUP = "other";

/**
 * The palette drawers, ordered.
 *
 * `groups` arrived with issue #10 and the runtime endpoint may serve an older
 * document, so the shape is reconstructed from the node types when it is
 * absent. A bundle newer than its Python must not produce an empty palette.
 */
export const GROUPS: readonly PaletteGroup[] =
  SCHEMA["x-brightbean"].groups ??
  [...new Set(NODE_TYPES.map((spec) => spec.group ?? FALLBACK_GROUP))].map((key) => ({
    key,
    label: key.charAt(0).toUpperCase() + key.slice(1),
  }));

const BY_TYPE: ReadonlyMap<string, NodeTypeSpec> = new Map(NODE_TYPES.map((spec) => [spec.type, spec]));

/** The spec for one node type, or `undefined` for a type this bundle predates. */
export function nodeSpec(type: string): NodeTypeSpec | undefined {
  return BY_TYPE.get(type);
}

export function groupOf(spec: NodeTypeSpec): string {
  return spec.group ?? FALLBACK_GROUP;
}

/**
 * The config schema for a node type, straight off `$defs.node_<type>`.
 *
 * Note this is returned unresolved: `condition`'s config is a bare `$ref` and
 * `smart_delay`'s is a bare tagged union, so a caller that reaches for
 * `.properties` breaks on two of the eleven types. Hand it to `deref()` and
 * then to the field dispatcher, which handles every shape.
 */
export function configSchema(type: string): JsonSchema | undefined {
  return SCHEMA.$defs[`node_${type}`]?.properties?.["config"];
}
