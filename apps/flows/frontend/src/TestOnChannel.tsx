/**
 * Run the draft against a real chat, on the channel the flow is built for.
 *
 * Which channel that is comes from the flow's own triggers and is decided by
 * the server (apps/channels/views_preview.py), so the button names it rather
 * than assuming Telegram — an Instagram automation whose only preview was
 * "Test on Telegram" could not be seen before it went to real customers.
 *
 * The button does not open anything by itself. Pressing it asks the server for
 * a fresh, short-lived deep link and then shows it, because the interesting
 * answers are the ones that are not a link — nothing connected yet, or a
 * channel with no live test at all — and a `window.open` that lands on an error
 * page is a worse way to say either than a sentence.
 *
 * The link is deliberately single-use-ish and expires in minutes
 * (apps/channels/preview.py), so it is minted on press rather than rendered
 * into the page: a link sitting in a toolbar the author left open for an hour
 * would be a link that no longer works.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "./api/client";
import { requestPreviewLink, type PreviewLink } from "./api/flows";
import { useBuilder } from "./store/context";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; link: PreviewLink & { ok: true } }
  | { kind: "blocked"; message: string; settingsUrl: string }
  | { kind: "error"; message: string };

export function TestOnChannel() {
  const { t } = useTranslation();
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
      setState({ kind: "blocked", message: result.message, settingsUrl: result.settings_url ?? "" });
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof ApiError ? error.message : t("testOnChannel.linkFailed"),
      });
    }
  };

  return (
    <span className="fb-test-channel inline-flex items-center gap-2">
      <button
        type="button"
        className="btn-link text-xs"
        disabled={state.kind === "loading"}
        onClick={() => void press()}
      >
        {state.kind === "loading" ? t("testOnChannel.preparing") : t("testOnChannel.testThisFlow")}
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
          {t("testOnChannel.openOn", { account: state.link.account, platform: state.link.platform_label })}
        </a>
      ) : null}

      {/*
        Telegram acts on the tap; Meta opens a composer and the referral rides
        in with the first message the tester sends. Saying so is the difference
        between a working link and a bug report about one.
      */}
      {state.kind === "ready" && state.link.instructions ? (
        <span className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          {state.link.instructions}
        </span>
      ) : null}

      {state.kind === "blocked" ? (
        <span className="fb-badge fb-badge-warning">
          {state.message}
          {state.settingsUrl ? (
            <>
              {" "}
              <a href={state.settingsUrl}>{t("testOnChannel.connectOne")}</a>
            </>
          ) : null}
        </span>
      ) : null}

      {state.kind === "error" ? <span className="fb-badge fb-badge-error">{state.message}</span> : null}
    </span>
  );
}
