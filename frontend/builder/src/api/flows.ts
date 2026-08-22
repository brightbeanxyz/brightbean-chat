/**
 * The four endpoints apps/flows/api.py serves, plus the media picker.
 *
 * Every URL is a data attribute on the mount div, so nothing here is
 * constructed — see src/env.ts.
 */
import type { BuilderEnv } from "../env";
import type { FlowDetail, FlowGraph, PickerPayload, SaveResult, StatsPayload } from "../schema/types";
import { request } from "./client";

export function loadFlow(env: BuilderEnv): Promise<FlowDetail> {
  return request<FlowDetail>(env.detailUrl);
}

/**
 * A 200 here means the draft was written. It may still carry `errors` — those
 * are graph-stage problems that block publish but not saving, because a draft
 * is allowed to be half-wired and an autosaving canvas that refused them would
 * throw away the user's work mid-edit.
 */
export function saveGraph(env: BuilderEnv, graph: FlowGraph): Promise<SaveResult> {
  return request<SaveResult>(env.detailUrl, { method: "PUT", body: { graph } });
}

export function publishFlow(env: BuilderEnv): Promise<SaveResult> {
  return request<SaveResult>(env.publishUrl, { method: "POST" });
}

export function fetchStats(env: BuilderEnv): Promise<StatsPayload> {
  return request<StatsPayload>(env.statsUrl);
}

export interface PickerQuery {
  q?: string;
  kind?: string;
  folder?: string;
  platform?: string;
  cursor?: string;
}

export function fetchPicker(env: BuilderEnv, query: PickerQuery = {}): Promise<PickerPayload> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value) {
      params.set(key, value);
    }
  }
  const suffix = params.toString();
  return request<PickerPayload>(suffix ? `${env.mediaPickerUrl}?${suffix}` : env.mediaPickerUrl);
}
