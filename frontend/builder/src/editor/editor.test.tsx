/**
 * The left column, and why it is one column rather than two panels.
 *
 * These cover the four things the rebuild was for, each of which was a
 * complaint about the old builder rather than a preference:
 *
 * - what starts the flow is visible without going looking for it;
 * - a step can be reached without hunting for its card on the canvas;
 * - a step's problems are next to the fields that fix them, not behind a tab;
 * - no error code reaches the reader.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AddStep } from "../canvas/AddStep";
import { nodeSpec } from "../schema/artifact";
import { Preview } from "../preview/Preview";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import type { TriggerSummary } from "../schema/types";
import { StepEditor } from "./StepEditor";
import { titleOf } from "./title";

function trigger(overrides: Partial<TriggerSummary> = {}): TriggerSummary {
  return {
    id: "t1",
    type: "comment",
    type_label: "Comment",
    enabled: true,
    priority: 10,
    summary: "Comments on any post",
    plain: "When someone comments on a post",
    connection: null,
    platforms: ["instagram"],
    ...overrides,
  };
}

const TEXT_NODE = {
  id: "n1",
  type: "send_message",
  position: { x: 0, y: 0 },
  config: { blocks: [{ type: "text", text: "Thanks for commenting!" }] },
};

function oneStep(triggers: TriggerSummary[] = []) {
  return makeDetail({ schema: 1, nodes: [TEXT_NODE], edges: [] }, { triggers });
}

describe("what starts the flow", () => {
  it("is on screen without opening anything", () => {
    renderWith(makeStore(oneStep([trigger()])), <StepEditor />);

    expect(screen.getByText("Comment")).toBeInTheDocument();
    expect(screen.getByText("Comments on any post")).toBeInTheDocument();
  });

  it("says so plainly when there is none, because that flow can never run", () => {
    renderWith(makeStore(oneStep()), <StepEditor />);

    expect(screen.getByText(/nothing starts this flow yet/i)).toBeInTheDocument();
  });

  it("says so when every trigger it has is switched off", () => {
    // How every imported template arrives. Without this the column shows a
    // trigger and reads as a working flow.
    renderWith(makeStore(oneStep([trigger({ enabled: false })])), <StepEditor />);

    expect(screen.getByText(/switched off, so nothing reaches this flow/i)).toBeInTheDocument();
  });

  it("edits through the Django drawer rather than a second editor of its own", () => {
    // The button dispatches the event templates/flows/edit.html listens for.
    // A React trigger editor would be a second place for the platform gate and
    // the config schemas to be wrong.
    const store = makeStore(oneStep([trigger()]));
    renderWith(store, <StepEditor />);
    let opened = 0;
    window.addEventListener("toggle-triggers", () => (opened += 1));

    fireEvent.click(screen.getByRole("button", { name: /change what starts it/i }));

    expect(opened).toBe(1);
  });
});

describe("the step list", () => {
  it("reaches a step without finding its card on the canvas", () => {
    const store = makeStore(oneStep([trigger()]));
    renderWith(store, <StepEditor />);

    fireEvent.click(screen.getByRole("button", { name: /Thanks for commenting/i }));

    expect(store.getState().selection.nodes).toEqual(["n1"]);
  });

  it("titles a step by what it says, not by its type", () => {
    // A flow with four sends was four rows reading "Send Message".
    expect(titleOf("send_message", TEXT_NODE.config)).toBe("Thanks for commenting!");
  });

  it("falls back to the type's label for a step with nothing in it yet", () => {
    // Read from the registry rather than typed out: the labels are copy now
    // ("Send a message", not "Send Message"), and a test that hard-codes one
    // fails on the next wording change without a bug behind it.
    expect(titleOf("send_message", { blocks: [] })).toBe(nodeSpec("send_message")?.label);
  });
});

describe("the seam between the list and the editor", () => {
  function selectStep() {
    const store = makeStore(oneStep([trigger()]));
    store.getState().setSelection({ nodes: ["n1"], edges: [] });
    return renderWith(store, <StepEditor />);
  }

  it("captions the editor, so its number is not read as one more row", () => {
    // The header repeated the selected row's own number, eyebrow and title in
    // the same components a few pixels beneath it, so the editor read as a
    // seventh row that started counting again at one.
    selectStep();

    expect(screen.getByText("Editing step 1")).toBeInTheDocument();
  });

  it("does not repeat the row's number or its kind", () => {
    const { container } = selectStep();

    // Scoped to the header, not the panel: the list row keeps its circle and
    // its eyebrow, and the trigger card above has an eyebrow of its own. What
    // must not happen is the header saying either of them a second time.
    const head = container.querySelector(".fb-edit-head");
    expect(head).not.toBeNull();
    expect(head?.querySelector(".fb-step-number")).toBeNull();
    expect(head?.querySelector(".fb-step-eyebrow")).toBeNull();
    expect(head?.textContent).not.toContain("Then send");
  });

  it("lets the title wrap instead of truncating it a second time", () => {
    // It is the thing being worked on, and the same sentence was being cut at
    // two different widths — once in the row, once in the header.
    const { container } = selectStep();

    const title = container.querySelector(".fb-edit-title");
    expect(title).not.toBeNull();
    expect(title?.className).not.toContain("truncate");
  });
});

describe("a step's problems", () => {
  it("are shown with the fields that fix them, and carry no error code", () => {
    const detail = makeDetail(
      { schema: 1, nodes: [{ ...TEXT_NODE, config: { blocks: [] } }], edges: [] },
      {
        validation: {
          errors: [{ code: "send_message_no_blocks", message: "This step has no message yet.", node_id: "n1" }],
          warnings: [],
        },
      },
    );
    const store = makeStore(detail);
    store.getState().setSelection({ nodes: ["n1"], edges: [] });

    const { container } = renderWith(store, <StepEditor />);

    expect(screen.getByText("This step has no message yet.")).toBeInTheDocument();
    expect(container.textContent).not.toContain("send_message_no_blocks");
  });
});

describe("adding a step", () => {
  it("offers the kinds grouped in the reader's words", () => {
    renderWith(makeStore(oneStep()), <AddStep />);

    fireEvent.click(screen.getByRole("button", { name: /add a step/i }));

    expect(screen.getByText("Then send")).toBeInTheDocument();
  });

  it("places the new step clear of the others and selects it", () => {
    // Clicking used to drop the node at the centre of the pane, usually on top
    // of one already there, and leave it unselected.
    const store = makeStore(oneStep());
    renderWith(store, <AddStep />);

    fireEvent.click(screen.getByRole("button", { name: /add a step/i }));
    const menu = screen.getByRole("menu");
    fireEvent.click(within(menu).getAllByRole("menuitem")[0] as HTMLElement);

    const state = store.getState();
    const added = state.nodeOrder.find((id) => id !== "n1") as string;
    expect(added).toBeDefined();
    expect(state.selection.nodes).toEqual([added]);
    expect((state.position[added] as { x: number }).x).toBeGreaterThan(TEXT_NODE.position.x);
  });
});

describe("the preview", () => {
  it("renders the selected step as the reader will see it", () => {
    const store = makeStore(oneStep());
    store.getState().setSelection({ nodes: ["n1"], edges: [] });

    renderWith(store, <Preview />);

    expect(screen.getByText("Thanks for commenting!")).toBeInTheDocument();
  });

  it("shows placeholders literally, because that is what the sender does", () => {
    // Inventing a name here would preview a different message, and evaluating
    // anything from a config is exactly what SECURITY-BASELINE §3 forbids.
    const detail = makeDetail(
      {
        schema: 1,
        nodes: [{ ...TEXT_NODE, config: { blocks: [{ type: "text", text: "Hi {{ first_name }}" }] } }],
        edges: [],
      },
      {},
    );
    const store = makeStore(detail);
    store.getState().setSelection({ nodes: ["n1"], edges: [] });

    renderWith(store, <Preview />);

    expect(screen.getByText("Hi {{ first_name }}")).toBeInTheDocument();
  });

  it("says what a step that sends nothing does, rather than drawing an empty phone", () => {
    const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
    const id = store.getState().nodeOrder.find((entry) => store.getState().nodeType[entry] === "smart_delay");
    store.getState().setSelection({ nodes: [id as string], edges: [] });

    renderWith(store, <Preview />);

    expect(screen.getByText(/does not send anything/i)).toBeInTheDocument();
  });
});
