/**
 * What the rail says, and what it deliberately does not.
 *
 * There was no test file here at all, which is how `no_entry_node` came to be
 * printed in monospace under an otherwise good sentence for a whole release.
 */
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProblemsRail } from "./ProblemsRail";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";

const withIssues = (errors: { code: string; message: string }[]) =>
  makeStore(makeDetail(makeSampleGraph(), { validation: { errors, warnings: [] } }));

describe("ProblemsRail", () => {
  it("shows the sentence and not the internal code", () => {
    const store = withIssues([{ code: "no_entry_node", message: "The flow has no nodes to run. Add one before publishing." }]);

    renderWith(store, <ProblemsRail />);

    expect(screen.getByText(/Add one before publishing/)).toBeTruthy();
    expect(screen.queryByText("no_entry_node")).toBeNull();
  });

  it("keeps the code reachable for support", () => {
    const store = withIssues([{ code: "no_entry_node", message: "The flow has no nodes to run." }]);

    const { container } = renderWith(store, <ProblemsRail />);

    expect(container.querySelector('[data-issue-code="no_entry_node"]')).toBeTruthy();
  });

  it("still shows one line for an issue that arrives once per offending node", () => {
    // multiple_entry_nodes is emitted per node by apps/flows/schema/validation.py.
    // railIssues dedupes on code|message from the data, so hiding the code
    // changes nothing about which issues survive.
    const store = withIssues([
      { code: "multiple_entry_nodes", message: "More than one node starts this flow." },
      { code: "multiple_entry_nodes", message: "More than one node starts this flow." },
    ]);

    renderWith(store, <ProblemsRail />);

    expect(screen.getAllByText("More than one node starts this flow.")).toHaveLength(1);
  });

  it("renders a code this bundle has never seen", () => {
    // The documented Layer 4/5 rule: "an error I cannot classify" must never
    // become "no error". Now that the code is not displayed, the message is the
    // only thing carrying it, which makes this rule more load-bearing, not less.
    const store = withIssues([{ code: "a_code_from_a_later_layer", message: "Something a later layer checks." }]);

    renderWith(store, <ProblemsRail />);

    expect(screen.getByText("Something a later layer checks.")).toBeTruthy();
  });
});
