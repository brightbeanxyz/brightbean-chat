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
import { AddTriggerCard, TriggerCard } from "./TriggerCard";
import { TriggerEdge } from "./TriggerEdge";
import { ADD_TRIGGER_NODE_TYPE, TRIGGER_EDGE_TYPE, TRIGGER_NODE_TYPE } from "./triggerNodes";

// The two synthetic types are registered alongside the generated ones. Their
// keys start with `__`, which no registered node type can, so neither can ever
// shadow a real one.
export const nodeTypes: NodeTypes = Object.freeze({
  ...Object.fromEntries(NODE_TYPES.map((spec) => [spec.type, FlowNodeCard])),
  [TRIGGER_NODE_TYPE]: TriggerCard,
  [ADD_TRIGGER_NODE_TYPE]: AddTriggerCard,
} as NodeTypes);

export const edgeTypes: EdgeTypes = Object.freeze({
  handleLabel: HandleLabelEdge,
  [TRIGGER_EDGE_TYPE]: TriggerEdge,
});
