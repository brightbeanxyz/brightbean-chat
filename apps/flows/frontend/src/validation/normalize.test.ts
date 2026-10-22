import { describe, expect, it } from "vitest";

import { configPath, indexIssues, railIssues, worstSeverity } from "./normalize";

describe("severity", () => {
  it("comes from which array the issue arrived in, not from a field", () => {
    // Issue.as_dict() does not serialise `stage`, so there is nothing to read.
    const index = indexIssues({
      errors: [{ code: "dangling_edge", message: "Edge goes nowhere.", edge_id: "e1" }],
      warnings: [{ code: "unreachable_node", message: "Never reached.", node_id: "n1" }],
    });

    expect(index.errors[0]?.severity).toBe("error");
    expect(index.warnings[0]?.severity).toBe("warning");
  });

  it("renders a code this bundle has never seen", () => {
    // Layers 4 and 5 add codes. "An error I cannot classify" must never become
    // "no error".
    const index = indexIssues({ errors: [{ code: "invented_in_l5", message: "Something new." }], warnings: [] });

    expect(index.graphLevel).toHaveLength(1);
    expect(index.graphLevel[0]?.severity).toBe("error");
  });
});

describe("indexing", () => {
  const index = indexIssues({
    errors: [
      { code: "missing_required_config", message: "a", node_id: "n1", path: "nodes[0].config.blocks[0].text" },
      { code: "no_entry_node", message: "No entry." },
      { code: "malformed_handle", message: "bad", edge_id: "e2", path: "edges[2].sourceHandle" },
    ],
    warnings: [{ code: "capability_unsupported", message: "c", node_id: "n1", path: "config.buttons" }],
  });

  it("files issues by node, by edge, and graph-level", () => {
    expect(index.byNode["n1"]).toHaveLength(2);
    expect(index.byEdge["e2"]).toHaveLength(1);
    expect(index.graphLevel.map((issue) => issue.code)).toEqual(["no_entry_node"]);
  });

  it("reports the worst severity for a node's badge", () => {
    expect(worstSeverity(index.byNode["n1"])).toBe("error");
    expect(worstSeverity(index.warnings)).toBe("warning");
    expect(worstSeverity([])).toBeNull();
  });
});

describe("configPath handles both roots the server uses", () => {
  it.each([
    // The schema validator's envelope paths…
    ["nodes[3].config.buttons[0].label", "buttons[0].label"],
    ["nodes[0].config", ""],
    // …and the capability warnings' config-relative ones.
    ["config.blocks[1].text", "blocks[1].text"],
    ["config", ""],
  ])("reduces %s to %s", (path, expected) => {
    expect(configPath(path)).toBe(expected);
  });

  it.each(["edges[2].sourceHandle", "schema", "nodes[3].position", undefined])(
    "leaves %s unattached rather than guessing",
    (path) => {
      expect(configPath(path)).toBeUndefined();
    },
  );
});

describe("the flow-level rail", () => {
  it("dedupes multiple_entry_nodes, which arrives once per offending node", () => {
    const index = indexIssues({
      errors: [
        { code: "multiple_entry_nodes", message: "More than one entry node.", node_id: "n1" },
        { code: "multiple_entry_nodes", message: "More than one entry node.", node_id: "n2" },
      ],
      warnings: [],
    });

    // Badged on both cards…
    expect(index.byNode["n1"]).toHaveLength(1);
    expect(index.byNode["n2"]).toHaveLength(1);
    // …but said once in the rail.
    expect(railIssues(index)).toHaveLength(1);
  });
});
