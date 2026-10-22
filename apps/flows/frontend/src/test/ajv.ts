/**
 * An independent oracle for "would the server accept this graph?".
 *
 * The builder's own resolver decides what to render; Ajv decides whether the
 * result validates. Using the builder's code to check the builder's output
 * would only prove it is self-consistent, which is exactly the bug class these
 * tests exist to catch.
 *
 * `strict: false` because `discriminator` is not a keyword Ajv knows in this
 * mode — the `oneOf` beside it carries the same meaning, so nothing is lost.
 */
import Ajv2020 from "ajv/dist/2020";
import type { ValidateFunction } from "ajv/dist/2020";

import { SCHEMA } from "../schema/artifact";
import type { FlowGraph } from "../schema/types";

const ajv = new Ajv2020({ strict: false, allErrors: true });
const validate: ValidateFunction = ajv.compile(SCHEMA as unknown as object);

export interface ValidationOutcome {
  valid: boolean;
  errors: string[];
}

export function validateGraph(graph: FlowGraph): ValidationOutcome {
  const valid = validate(graph) as boolean;
  return {
    valid,
    errors: (validate.errors ?? []).map((error) => `${error.instancePath || "/"} ${error.message ?? ""}`.trim()),
  };
}

/** Validate one node in isolation, which gives a far more readable failure. */
export function validateNode(node: unknown): ValidationOutcome {
  return validateGraph({ schema: SCHEMA["x-brightbean"].schema_version, nodes: [node as never], edges: [] });
}
