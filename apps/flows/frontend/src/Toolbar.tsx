/**
 * The builder's top bar (HANDOFF §3, Flow builder): one 56px row that replaced
 * the page header and the old toolbar under it. The way back and the flow's
 * name — edited in place — with its status and save state on the left; undo,
 * redo, stats, Test, the problems count, Publish and the downloads behind "…"
 * on the right. Publish is the bar's only orange control.
 *
 * Two pieces of copy here are load-bearing. "Saved" and "N problems" are shown
 * side by side rather than folded into one status, because a 200 from the API
 * means the draft *was written* and may still carry graph-stage errors — a
 * draft is allowed to be half-wired. And Publish stays enabled with known
 * errors: what the builder knows is only as of the last save, so disabling it
 * would be a claim it cannot support.
 *
 * An unchanged published version needs no repeat publish, so the same button
 * becomes "Set offline" — unless all its configured triggers are off, in which
 * case the action turns them on, and the status says plainly why the published
 * flow cannot start yet.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "./api/client";
import { TestOnChannel } from "./TestOnChannel";
import type { ValidationPayload } from "./schema/types";
import { loadFlow, publishFlow, renameFlow, takeFlowOffline } from "./api/flows";
import i18n from "./i18n";
import { offlineHint, publishView, type PublishAction } from "./publishState";
import { refreshApplies } from "./refreshState";
import { showToast } from "./toast";
import type { Autosave } from "./persistence/autosave";
import { useBuilder, useBuilderStore } from "./store/context";
import { ProblemsRail } from "./validation/ProblemsRail";

// i18n.t(), not a module-level table: these labels have to read in whatever
// language is active *now*, the same reason publishState.ts's own copy does.
function busyLabel(action: PublishAction): string {
  return {
    publish: i18n.t("toolbar.settingLive"),
    enable: i18n.t("toolbar.turningOn"),
    offline: i18n.t("toolbar.settingOffline"),
  }[action];
}

function stoppedCopy(stopped: number): string {
  if (stopped === 0) {
    return i18n.t("toolbar.stoppedNone");
  }
  return i18n.t("toolbar.stoppedCount", { count: stopped });
}

export function Toolbar({ autosave }: { autosave: Autosave | null }) {
  const { t } = useTranslation();
  const store = useBuilderStore();
  const save = useBuilder((state) => state.save);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const statsVisible = useBuilder((state) => state.statsVisible);
  const statsFailed = useBuilder((state) => state.statsFailed);
  const canUndo = useBuilder((state) => state.past.length > 0);
  const canRedo = useBuilder((state) => state.future.length > 0);
  const errorCount = useBuilder((state) => state.validation.errors.length);
  const warningCount = useBuilder((state) => state.validation.warnings.length);
  const flowStatus = useBuilder((state) => state.flow?.status);
  const env = useBuilder((state) => state.env);
  const triggers = useBuilder((state) => state.triggers);
  const view = publishView(save, flowStatus, triggers.length, triggers.filter((trigger) => trigger.enabled).length);
  // Which action is in flight, not just whether one is: two buttons can start
  // one, and the busy label belongs on the one that was clicked — not on
  // whichever button the response has since relabelled.
  const [busy, setBusy] = useState<PublishAction | null>(null);

  const publish = async () => {
    const enablingOnly = view.action === "enable";
    setBusy(view.action);
    try {
      // Flush first, and stop if it did not land. Publishing a draft the server
      // has not seen publishes the *previous* version — and then reports
      // success, which is worse than doing nothing.
      if (autosave && !(await autosave.flush())) {
        store.getState().setSave({ message: t("toolbar.flushFailed") });
        return;
      }
      const result = await publishFlow(store.getState().env);
      store.getState().applyValidation(result.validation, store.getState().revision);
      // The flow itself, not just the save slice. services.publish() moves a
      // draft *or an archived* flow to active, and the response carries the
      // status it landed on — dropping it left the store reading "archived",
      // so the header this button sits in went on offering Publish for a flow
      // that had just gone live.
      store.getState().setFlow(result.flow);
      store.getState().setTriggers(result.triggers);
      store.getState().setSave({
        state: "saved",
        version: result.version,
        publishedVersion: result.version,
        message: null,
        issues: [],
      });
      // Report the action where the person pressed it, as well as in status.
      showToast({
        tone: "success",
        title: enablingOnly ? t("toolbar.triggersTurnedOn") : t("toolbar.publishedTitle"),
        body: enablingOnly ? t("toolbar.allTriggersOn") : t("toolbar.publishedBody", { version: result.version.version }),
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) {
        const payload = error.payload as { validation?: ValidationPayload } | null;
        if (payload?.validation) {
          store.getState().applyValidation(payload.validation, store.getState().revision);
        }
        store.getState().setSave({ message: t("toolbar.publishRejected") });
      } else if (error instanceof ApiError) {
        store.getState().setSave({ message: error.message });
      }
    } finally {
      setBusy(null);
    }
  };

  const takeOffline = async () => {
    // Asked first because it cannot be taken back: the conversations it stops
    // stay stopped when the flow is set live again.
    if (!window.confirm(t("toolbar.offlineConfirm"))) {
      return;
    }
    setBusy("offline");
    // Captured before the request, compared after it: the button is offered
    // beside a pending draft too, and a version from the server must not
    // overwrite the save slice of an edit it has not seen (refreshState.ts).
    const revision = store.getState().revision;
    try {
      // No flush. Going offline does not depend on the draft, and a draft that
      // fails validation must not stand between a live flow and switching it off.
      const result = await takeFlowOffline(store.getState().env);
      store.getState().setFlow(result.flow);
      store.getState().setTriggers(result.triggers);
      const state = store.getState();
      store.getState().setSave(
        refreshApplies(state.save.state, revision, state.revision)
          ? { version: result.version, publishedVersion: null, message: null }
          : { publishedVersion: null, message: null },
      );
      showToast({ tone: "success", title: t("toolbar.flowIsOffline"), body: stoppedCopy(result.stopped) });
    } catch (error) {
      if (error instanceof ApiError) {
        store.getState().setSave({ message: error.message });
      }
      if (error instanceof ApiError && error.status === 409) {
        // The flow is not what this page thinks: another tab or member took it
        // offline or archived it. Re-read it, or the toolbar goes on offering
        // the same refused action. Status and triggers are not the graph, so
        // they always apply; versions only when no edit raced the request.
        void loadFlow(store.getState().env)
          .then((detail) => {
            store.getState().setFlow(detail.flow);
            store.getState().setTriggers(detail.triggers);
            const now = store.getState();
            if (refreshApplies(now.save.state, revision, now.revision)) {
              now.setSave({ version: detail.version, publishedVersion: detail.published_version });
            }
          })
          .catch(() => {});
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="fb-toolbar" role="toolbar" aria-label="Flow">
      {/* The way back and the flow's own name and state, on the left. */}
      <div className="fb-toolbar-identity">
        {env.listUrl ? (
          <a className="fb-toolbar-back" href={env.listUrl}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>
            {t("toolbar.flows")}
          </a>
        ) : null}
        {env.listUrl ? <span className="fb-toolbar-sep" aria-hidden="true">/</span> : null}
        <FlowName />
        <span className={`fb-flow-status fb-flow-status-${view.statusTone}`} aria-live="polite">
          <span className="fb-flow-status-dot" aria-hidden="true" />
          {view.statusLabel}
        </span>
        {canEdit ? (
          <span className="fb-save-state" data-save-state={save.state}>
            {view.saveLabel}
          </span>
        ) : (
          <span className="fb-save-state">{t("toolbar.readOnly")}</span>
        )}
      </div>

      <div className="fb-toolbar-actions">
        {canEdit ? (
          <>
            <button type="button" className="fb-toolbar-icon" aria-label={t("toolbar.undo")} title={t("toolbar.undo")} disabled={!canUndo} onClick={() => store.getState().undo()}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-2"/></svg>
            </button>
            <button type="button" className="fb-toolbar-icon" aria-label={t("toolbar.redo")} title={t("toolbar.redo")} disabled={!canRedo} onClick={() => store.getState().redo()}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 14 5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h2"/></svg>
            </button>
          </>
        ) : null}

        <button
          type="button"
          className="fb-toolbar-icon"
          aria-label={statsVisible ? t("toolbar.hideStats") : t("toolbar.showStats")}
          title={statsVisible ? t("toolbar.hideStats") : t("toolbar.showStats")}
          aria-pressed={statsVisible}
          onClick={() => store.getState().toggleStats()}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V11M10 20V5M16 20v-8M22 20V8"/></svg>
        </button>

        {statsFailed ? <span className="fb-badge fb-badge-warning">{t("toolbar.statsUnavailable")}</span> : null}

        {/*
          Editors only. Testing runs the *draft* against a real chat and sends
          real messages, which is an edit-shaped act however read-only the
          surrounding canvas looks; the server enforces `edit_flows` on the
          endpoint either way.
        */}
        {canEdit ? <TestOnChannel /> : null}

        <ProblemsMenu errorCount={errorCount} warningCount={warningCount} />

        {canEdit ? (
          <button
            type="button"
            // Secondary when it switches the flow off: the same spot as the
            // call to action, but not dressed as one.
            className={view.action === "offline" ? "btn-pill-secondary btn-pill-sm" : "btn-pill-primary btn-pill-sm"}
            disabled={busy !== null}
            title={view.publishHint ?? undefined}
            onClick={() => void (view.action === "offline" ? takeOffline() : publish())}
          >
            {busy && busy === view.action ? busyLabel(busy) : view.publishLabel}
          </button>
        ) : null}
        {canEdit && view.secondary === "offline" ? (
          <button
            type="button"
            className="btn-pill-secondary btn-pill-sm"
            disabled={busy !== null}
            title={offlineHint()}
            onClick={() => void takeOffline()}
          >
            {busy === "offline" ? busyLabel("offline") : t("toolbar.setOffline")}
          </button>
        ) : null}

        <OverflowMenu />
      </div>
    </div>
  );
}

/**
 * The flow's name, edited where it is shown (HANDOFF §3: "inline-editable
 * name"). A button until clicked, so it reads as a title and not as a field;
 * Enter or leaving the field saves, Escape puts it back. A blank name is not
 * sent — the server would refuse it, and the old name is the better answer.
 */
function FlowName() {
  const { t } = useTranslation();
  const store = useBuilderStore();
  const flow = useBuilder((state) => state.flow);
  const env = useBuilder((state) => state.env);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const name = flow?.name || env.flowName || t("toolbar.untitledFlow");
  const canRename = env.canEdit && Boolean(env.renameUrl);

  const commit = async () => {
    const next = draft.trim();
    setEditing(false);
    if (!flow || !next || next === flow.name) {
      return;
    }
    // Optimistic: the bar shows the new name at once, and the old one comes
    // back with the reason if the server says no.
    store.getState().setFlow({ ...flow, name: next });
    try {
      const result = await renameFlow(env, next);
      store.getState().setFlow(result.flow);
    } catch (error) {
      store.getState().setFlow(flow);
      showToast({
        tone: "error",
        title: t("toolbar.notRenamed"),
        body: error instanceof ApiError ? error.message : t("toolbar.nameNotSaved"),
      });
    }
  };

  if (editing) {
    return (
      <input
        className="fb-flow-name-input"
        aria-label={t("toolbar.flowName")}
        value={draft}
        maxLength={200}
        autoFocus
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => void commit()}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.currentTarget.blur();
          } else if (event.key === "Escape") {
            setEditing(false);
          }
        }}
      />
    );
  }
  return canRename ? (
    <button
      type="button"
      className="fb-flow-name"
      title={t("toolbar.renameThisFlow")}
      onClick={() => {
        setDraft(name);
        setEditing(true);
      }}
    >
      <h1 className="fb-flow-name-text">{name}</h1>
    </button>
  ) : (
    <h1 className="fb-flow-name-text">{name}</h1>
  );
}

/** Close a popover on a click outside it or on Escape. */
function useDismiss(open: boolean, ref: { current: HTMLElement | null }, close: () => void) {
  useEffect(() => {
    if (!open) {
      return;
    }
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        close();
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        close();
      }
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, ref, close]);
}

/**
 * The problems, behind one button that always says how many there are
 * (HANDOFF §3: "Problems … becomes a popover from the problems button"). The
 * list used to be a rail along the bottom of the canvas, permanently taking
 * room from it whether or not anything was wrong. The count is always in
 * view; the list is a click away, and opens by itself when a Set live is
 * refused, because that is the moment someone needs to read it.
 */
function ProblemsMenu({ errorCount, warningCount }: { errorCount: number; warningCount: number }) {
  const { t } = useTranslation();
  const message = useBuilder((state) => state.save.message);
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, wrapper, close);

  useEffect(() => {
    if (message) {
      setOpen(true);
    }
  }, [message]);

  const tone = errorCount > 0 ? "error" : warningCount > 0 ? "warning" : "ok";
  const label =
    errorCount > 0
      ? t("toolbar.toFix", { count: errorCount })
      : warningCount > 0
        ? t("toolbar.toCheck", { count: warningCount })
        : t("toolbar.ready");

  return (
    <div className="fb-problems-menu" ref={wrapper}>
      <button
        type="button"
        className={`fb-problems-button fb-problems-button-${tone}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        <span className="fb-problems-dot" aria-hidden="true" />
        {label}
      </button>
      {open ? (
        <div className="fb-problems-popover" role="dialog" aria-label={t("toolbar.problems")}>
          <ProblemsRail onPick={close} />
          {errorCount === 0 && warningCount === 0 && !message ? (
            <p className="fb-empty">{t("toolbar.nothingToFix")}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The actions people need now and then rather than every minute — the two
 * downloads — behind "…", where the HANDOFF puts Export. Plain links: the
 * response is a file.
 */
function OverflowMenu() {
  const { t } = useTranslation();
  const env = useBuilder((state) => state.env);
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, wrapper, close);

  if (!env.exportUrl && !env.exportBundleUrl) {
    return null;
  }
  return (
    <div className="fb-overflow" ref={wrapper}>
      <button
        type="button"
        className="fb-toolbar-icon"
        aria-label={t("toolbar.moreActions")}
        title={t("toolbar.moreActions")}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.2" /><circle cx="12" cy="12" r="1.2" /><circle cx="19" cy="12" r="1.2" /></svg>
      </button>
      {open ? (
        <div className="fb-overflow-menu" role="menu">
          {env.exportUrl ? (
            <a role="menuitem" href={env.exportUrl} onClick={close}>{t("toolbar.downloadCopy")}</a>
          ) : null}
          {env.exportBundleUrl ? (
            <a role="menuitem" href={env.exportBundleUrl} onClick={close}>{t("toolbar.downloadBundle")}</a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
