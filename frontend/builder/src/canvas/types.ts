/**
 * React Flow's type maps, built once at module scope.
 *
 * Recreating either object on a render is the classic @xyflow mistake: React
 * Flow compares them by identity, warns, and remounts every node on the canvas
 * — which is both a visible flicker and a guaranteed failure of the
 * "100 nodes stay responsive" criterion.
 *
 * Built from the artefact, so a node type registered by a later layer is
 * renderable with no edit here.
 */
import type { EdgeTypes, NodeTypes } from "@xyflow/react";

import { NODE_TYPES } from "../schema/artifact";
import { FlowNodeCard } from "./FlowNodeCard";
import { HandleLabelEdge } from "./HandleLabelEdge";

export const nodeTypes: NodeTypes = Object.freeze(
  Object.fromEntries(NODE_TYPES.map((spec) => [spec.type, FlowNodeCard])),
);

export const edgeTypes: EdgeTypes = Object.freeze({ handleLabel: HandleLabelEdge });
