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

/**
 * SPEC §16's preview link.
 *
 * A 200 either way: "you have no Instagram account connected" is an ordinary
 * state for the builder to render, not a request that failed, and a 4xx would
 * send this down the API-error path and show a failure instead of an
 * explanation. `unsupported_platform` arrives the same way — a flow that only
 * runs on WhatsApp has no live test, and that is a fact about the channel
 * rather than something the reader has got wrong.
 *
 * `settings_url` is absent on `unsupported_platform`: there is nothing to go
 * and connect that would change the answer.
 */
export type PreviewLink =
  | {
      ok: true;
      deep_link: string;
      platform: string;
      platform_label: string;
      account: string;
      /** How to actually start the test, where tapping the link is not enough. */
      instructions: string;
      expires_in: number;
    }
  | { ok: false; reason: string; message: string; settings_url?: string };

export function requestPreviewLink(env: BuilderEnv): Promise<PreviewLink> {
  return request<PreviewLink>(env.previewUrl, { method: "POST" });
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
