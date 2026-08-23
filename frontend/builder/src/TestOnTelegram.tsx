/**
 * SPEC §16's "test on Telegram": run the draft against a real chat.
 *
 * The button does not open anything by itself. Pressing it asks the server for
 * a fresh, short-lived deep link and then shows it, because the interesting
 * failure is the one where the workspace has no Telegram bot connected — and a
 * `window.open` that lands on an error page is a worse way to say that than a
 * sentence with a link to the connect flow.
 *
 * The link is deliberately single-use-ish and expires in minutes
 * (apps/channels/preview.py), so it is minted on press rather than rendered
 * into the page: a link sitting in a toolbar the author left open for an hour
 * would be a link that no longer works.
 */
import { useState } from "react";

import { ApiError } from "./api/client";
import { requestPreviewLink, type PreviewLink } from "./api/flows";
import { useBuilder } from "./store/context";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; link: PreviewLink & { ok: true } }
  | { kind: "blocked"; message: string; settingsUrl: string }
  | { kind: "error"; message: string };

export function TestOnTelegram() {
  const env = useBuilder((state) => state.env);
  const [state, setState] = useState<State>({ kind: "idle" });

  const press = async () => {
    setState({ kind: "loading" });
    try {
      const result = await requestPreviewLink(env);
      if (result.ok) {
        setState({ kind: "ready", link: result });
        return;
      }
      setState({ kind: "blocked", message: result.message, settingsUrl: result.settings_url });
    } catch (error) {
      setState({
        kind: "error",
        message:
          error instanceof ApiError ? error.message : "The test link could not be created. Try again.",
      });
    }
  };

  return (
    <span className="fb-test-telegram inline-flex items-center gap-2">
      <button
        type="button"
        className="btn-link text-xs"
        disabled={state.kind === "loading"}
        onClick={() => void press()}
      >
        {state.kind === "loading" ? "Preparing…" : "Test on Telegram"}
      </button>

      {state.kind === "ready" ? (
        <a
          className="btn-link text-xs"
          href={state.link.deep_link}
          target="_blank"
          // noopener/noreferrer on a target=_blank link that leaves the app:
          // without it the opened tab gets a handle on this one via
          // window.opener.
          rel="noopener noreferrer"
        >
          Open {state.link.bot} →
        </a>
      ) : null}

      {state.kind === "blocked" ? (
        <span className="fb-badge fb-badge-warning">
          {state.message} <a href={state.settingsUrl}>Connect Telegram</a>
        </span>
      ) : null}

      {state.kind === "error" ? <span className="fb-badge fb-badge-error">{state.message}</span> : null}
    </span>
  );
}
