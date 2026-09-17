/**
 * The node's Problems tab leaks nothing either.
 *
 * The same code-printing line existed in two places. Fixing only the rail would
 * have left the banner clean and the machine key one click away.
 */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Inspector } from "./Inspector";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";

describe("the node Problems tab", () => {
  it("does not print the code either", () => {
    const graph = makeSampleGraph();
    const nodeId = graph.nodes[0]!.id;
    const store = makeStore(
      makeDetail(graph, {
        validation: {
          errors: [{ code: "config_invalid", message: "This node needs a message.", node_id: nodeId }],
          warnings: [],
        },
      }),
    );
    store.getState().setSelection({ nodes: [nodeId], edges: [] });

    const { container } = renderWith(store, <Inspector />);
    // The inspector opens on Config; the problem list is behind its own tab.
    fireEvent.click(screen.getByRole("button", { name: /Problems/ }));

    expect(screen.getByText("This node needs a message.")).toBeTruthy();
    expect(screen.queryByText("config_invalid")).toBeNull();
    expect(container.querySelector('[data-issue-code="config_invalid"]')).toBeTruthy();
  });
});
