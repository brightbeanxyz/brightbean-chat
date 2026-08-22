/**
 * The build-time import of static/flows/flow-schema.json.
 *
 * Declared by hand rather than through `resolveJsonModule`, which would make
 * tsc infer one enormous literal type for a 43 KB document — slow, and it would
 * give every node type's config schema a distinct anonymous type that nothing
 * can be written against.
 *
 * frontend/builder/vite.config.mts aliases the specifier to the committed
 * artefact; tsconfig.json points it here.
 */
declare module "@flow-schema" {
  import type { FlowSchemaDocument } from "./types";

  const document: FlowSchemaDocument;
  export default document;
}
