/**
 * The wire contracts, hand-written against their Python originals.
 *
 * Each type names the module it mirrors. They are the frontend's half of
 * ROADMAP contract 2 and should be reviewed against those files the way the
 * panels are — a drift here shows up as a 422 from an autosave, not as a
 * compile error.
 */

// ── The graph envelope: apps/flows/schema/envelope.py ────────────────────────

export interface Position {
  x: number;
  y: number;
}

/** Exactly SPEC §9.1's node shape, and nothing else. */
export interface DomainNode {
  id: string;
  type: string;
  position: Position;
  config: unknown;
}

/** Note there is no `targetHandle`: the schema does not have one. */
export interface DomainEdge {
  id: string;
  source: string;
  sourceHandle: string;
  target: string;
}

export interface FlowGraph {
  schema: number;
  nodes: DomainNode[];
  edges: DomainEdge[];
}

export interface GraphLimits {
  schema_version: number;
  max_graph_bytes: number;
  max_graph_depth: number;
  max_nodes: number;
  max_edges: number;
}

// ── The schema artefact: apps/flows/schema/export.py ─────────────────────────

/** A JSON Schema node, loose enough for the subset the artefact actually uses. */
export interface JsonSchema {
  $ref?: string;
  type?: string | string[];
  const?: unknown;
  enum?: unknown[];
  title?: string;
  description?: string;
  default?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean;
  items?: JsonSchema;
  minItems?: number;
  maxItems?: number;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  pattern?: string;
  oneOf?: JsonSchema[];
  anyOf?: JsonSchema[];
  discriminator?: { propertyName: string; mapping: Record<string, string> };
}

export interface DynamicHandleSpec {
  prefix: string;
  config_key: string;
}

export interface NodeTypeSpec {
  type: string;
  label: string;
  description: string;
  /** Added by issue #10. Optional here because the runtime endpoint may be older. */
  group?: string;
  handles: string[];
  dynamic_handles: DynamicHandleSpec[];
  terminal: boolean;
  annotation: boolean;
}

export interface PaletteGroup {
  key: string;
  label: string;
}

export interface FlowSchemaDocument {
  $schema: string;
  $id: string;
  title: string;
  type: string;
  required: string[];
  properties: Record<string, JsonSchema>;
  $defs: Record<string, JsonSchema>;
  "x-brightbean": {
    schema_version: number;
    limits: GraphLimits;
    /** Added by issue #10; absent from an older runtime document. */
    groups?: PaletteGroup[];
    node_types: NodeTypeSpec[];
  };
}

// ── The data API: apps/flows/api.py ──────────────────────────────────────────

export interface FlowMeta {
  id: string;
  name: string;
  status: string;
  folder: string;
  updated_at: string;
}

export interface VersionMeta {
  id: string;
  version: number;
  published: boolean;
  updated_at: string;
}

/** apps/flows/picklists.py always returns all six keys, in this order. */
export interface Picklists {
  tags: { id: string; label: string }[];
  custom_fields: { id: string; label: string; type: string }[];
  sequences: { id: string; label: string }[];
  flows: { id: string; label: string; status: string }[];
  connections: { id: string; label: string; platform: string; status: string }[];
  members: { id: string; label: string; email: string; role: string }[];
}

/**
 * apps/flows/schema/issues.py. `stage` is deliberately not serialised, so
 * severity is which array the issue arrived in and nothing else — see
 * ValidationPayload.
 */
export interface Issue {
  code: string;
  message: string;
  node_id?: string;
  edge_id?: string;
  path?: string;
}

export interface ValidationPayload {
  errors: Issue[];
  warnings: Issue[];
}

export interface FlowDetail {
  flow: FlowMeta;
  version: VersionMeta | null;
  graph: FlowGraph;
  published_version: VersionMeta | null;
  picklists: Picklists;
  validation: ValidationPayload;
  limits: GraphLimits;
  schema_url: string;
}

export interface SaveResult {
  flow: FlowMeta;
  version: VersionMeta;
  validation: ValidationPayload;
}

export interface NodeStats {
  sent: number;
  delivered: number;
  failed: number;
  clicked: number;
}

export interface StatsPayload {
  flow: { id: string };
  available: boolean;
  nodes: Record<string, NodeStats>;
  totals: NodeStats;
}

// ── The media picker: apps/media_library/picker.py ───────────────────────────

export interface MediaAsset {
  id: string;
  kind: "image" | "audio" | "video" | "file";
  mime: string;
  filename: string;
  title: string;
  alt_text: string;
  size: number;
  width: number;
  height: number;
  folder_id: string | null;
  url: string;
  thumbnail_url: string | null;
  created_at: string;
  platform_warnings: string[];
}

export interface MediaFolder {
  id: string;
  name: string;
  parent_id: string | null;
}

export interface PickerPayload {
  results: MediaAsset[];
  folders: MediaFolder[];
  next_cursor: string | null;
}
