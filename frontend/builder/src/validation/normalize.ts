/**
 * Turning the server's two issue arrays into something the canvas can index.
 *
 * Three things about the wire format shape this module:
 *
 * 1. **Severity is which array the issue arrived in.** `Issue.as_dict()` does
 *    not serialise `stage`, so there is no field to read and no code-prefix
 *    heuristic to invent.
 * 2. **"Saved" and "valid" are different questions**, and only the HTTP status
 *    answers the first. A 200 can carry `errors` — a draft is allowed to be
 *    half-wired — while a 422 means nothing was written. That distinction lives
 *    in the persistence layer; here every issue is just indexed.
 * 3. **An unknown code still renders.** Layer 4 and Layer 5 will add codes this
 *    bundle has never seen, and "an error I cannot classify" must not become
 *    "no error".
 */
import type { Issue, ValidationPayload } from "../schema/types";

export type Severity = "error" | "warning";

export interface NormalizedIssue extends Issue {
  severity: Severity;
  /**
   * `path` reduced to a config-relative pointer, or `undefined` when it does
   * not address a config field. See `configPath` for why this is not a slice.
   */
  fieldPath?: string;
}

export interface ValidationIndex {
  errors: NormalizedIssue[];
  warnings: NormalizedIssue[];
  byNode: Record<string, NormalizedIssue[]>;
  byEdge: Record<string, NormalizedIssue[]>;
  /** Issues addressing the graph as a whole — `no_entry_node`, `graph_too_large`. */
  graphLevel: NormalizedIssue[];
}

export function emptyValidationIndex(): ValidationIndex {
  return { errors: [], warnings: [], byNode: {}, byEdge: {}, graphLevel: [] };
}

const ENVELOPE_PREFIX = /^(?:nodes|edges)\[\d+\]\.?/;

/**
 * The config-relative pointer a `path` addresses, if any.
 *
 * Paths arrive with two different roots and the server does not say which:
 *
 * * envelope paths from the schema validator — `nodes[3].config.buttons[0].label`,
 *   `edges[2].sourceHandle`, `schema`
 * * config-relative paths from the capability warnings — `config.blocks[1].text`
 *
 * Both reduce to the same pointer by stripping whichever prefixes are present.
 * A path matching neither shape is returned as `undefined` rather than guessed
 * at, so the rail renders it as text instead of attaching it to a field it may
 * have nothing to do with.
 */
export function configPath(path: string | undefined): string | undefined {
  if (!path) {
    return undefined;
  }
  const withoutEnvelope = path.replace(ENVELOPE_PREFIX, "");
  if (withoutEnvelope === "config") {
    return "";
  }
  if (withoutEnvelope.startsWith("config.")) {
    return withoutEnvelope.slice("config.".length);
  }
  // `edges[2].sourceHandle`, `schema`, `nodes[3].position` — real paths, but
  // not into a node's config.
  return undefined;
}

function normalize(issue: Issue, severity: Severity): NormalizedIssue {
  const fieldPath = configPath(issue.path);
  return fieldPath === undefined ? { ...issue, severity } : { ...issue, severity, fieldPath };
}

export function indexIssues(payload: ValidationPayload | undefined): ValidationIndex {
  const index = emptyValidationIndex();
  if (!payload) {
    return index;
  }

  const all = [
    ...(payload.errors ?? []).map((issue) => normalize(issue, "error")),
    ...(payload.warnings ?? []).map((issue) => normalize(issue, "warning")),
  ];

  for (const issue of all) {
    (issue.severity === "error" ? index.errors : index.warnings).push(issue);

    if (issue.node_id) {
      (index.byNode[issue.node_id] ??= []).push(issue);
    } else if (issue.edge_id) {
      (index.byEdge[issue.edge_id] ??= []).push(issue);
    } else {
      index.graphLevel.push(issue);
    }
  }
  return index;
}

/** The worst severity among a node's issues, for the card's badge. */
export function worstSeverity(issues: readonly NormalizedIssue[] | undefined): Severity | null {
  if (!issues || issues.length === 0) {
    return null;
  }
  return issues.some((issue) => issue.severity === "error") ? "error" : "warning";
}

/**
 * The flow-level list, deduplicated.
 *
 * `multiple_entry_nodes` arrives once per offending node — right for badging
 * each card, wrong for a rail that would then repeat one sentence three times.
 */
export function railIssues(index: ValidationIndex): NormalizedIssue[] {
  const seen = new Set<string>();
  return [...index.errors, ...index.warnings].filter((issue) => {
    const key = `${issue.code}|${issue.message}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

/** Issues attached to one field of one node, for an inline message. */
export function issuesForField(
  issues: readonly NormalizedIssue[] | undefined,
  path: string,
): NormalizedIssue[] {
  return (issues ?? []).filter((issue) => issue.fieldPath === path);
}
