/**
 * What starts this flow, at the top of the left column.
 *
 * It used to be behind a "When it runs" button in the page header, which put
 * the single most important fact about a flow — whether anything will ever
 * reach it — two clicks away and invisible until you went looking. A flow with
 * no trigger looked exactly like a flow with one.
 *
 * **Editing still belongs to the Django drawer.** `templates/flows/_triggers_panel.html`
 * and the `flows:trigger_*` routes already create, bind, reorder and toggle
 * triggers, with the platform gate and the config schemas behind them. A second
 * editor in React would be a second place for that to be wrong, so this is a
 * display plus a door: the button dispatches the same `toggle-triggers` event
 * the header used to, and the drawer answers.
 *
 * The store's trigger list is refreshed on `triggersChanged` (see App.tsx), so
 * closing the drawer updates this without a reload.
 */
import { useBuilder } from "../store/context";

function openDrawer() {
  window.dispatchEvent(new CustomEvent("toggle-triggers", { bubbles: true }));
}

export function TriggerSection() {
  const triggers = useBuilder((state) => state.triggers);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const selected = useBuilder((state) => state.triggerSelected);
  const status = useBuilder((state) => state.flow?.status);
  const published = status === "active";
  const enabled = triggers.filter((trigger) => trigger.enabled);

  return (
    // Highlighted when the canvas card is selected, the same way a step's row
    // highlights — so clicking either one shows you which is which.
    <section className={selected ? "fb-trigger-section is-selected" : "fb-trigger-section"}>
      <h2 className="fb-trigger-heading">When it runs</h2>

      {triggers.length === 0 ? (
        <>
          <p className="fb-trigger-empty">Nothing starts this flow yet, so it will not run.</p>
          {canEdit ? (
            <button type="button" className="btn-pill-primary btn-pill-sm mt-2.5" onClick={openDrawer}>
              Choose what starts it
            </button>
          ) : null}
        </>
      ) : (
        <>
          <ul className="fb-trigger-list">
            {triggers.map((trigger) => (
              <li key={trigger.id} className={trigger.enabled ? "fb-trigger-row" : "fb-trigger-row is-off"}>
                <span className="fb-trigger-name">{trigger.type_label}</span>
                {!trigger.enabled ? <span className="fb-trigger-off">Switched off</span> : null}
                <span className="fb-trigger-detail block">{trigger.summary}</span>
              </li>
            ))}
          </ul>
          {status === "offline" ? (
            // The list above still reads as "this is what starts it", and for
            // an offline flow none of it does — the toolbar pill is easy to miss.
            <p className="fb-trigger-empty mt-2">
              This flow is offline, so none of these start it. They will again once it is set live.
            </p>
          ) : enabled.length === 0 ? (
            <p className="fb-trigger-empty mt-2">
              {published
                ? "This flow is published, but every trigger is off. It will not start until one is turned on."
                : "Every trigger is off, so nothing reaches this flow yet."}
            </p>
          ) : null}
          {canEdit ? (
            <button type="button" className="btn-pill-secondary btn-pill-sm mt-2.5" onClick={openDrawer}>
              Manage triggers
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
