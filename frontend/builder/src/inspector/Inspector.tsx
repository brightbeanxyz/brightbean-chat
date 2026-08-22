/**
 * The right-hand drawer.
 *
 * It subscribes to `config[selectedId]` and nothing broader, which is what keeps
 * a drag from re-rendering it — the criterion about a hundred-node graph is a
 * property of these three `useBuilder` calls as much as of the store's shape.
 */
import { useMemo, useState } from "react";

import { configSchema, nodeSpec } from "../schema/artifact";
import { useBuilder, useBuilderStore } from "../store/context";
import type { ConfigPath } from "../store/paths";
import { worstSeverity } from "../validation/normalize";
import { FieldProvider, type FieldContextValue } from "./FieldContext";
import { SchemaField } from "./SchemaField";

export function Inspector() {
  const store = useBuilderStore();
  const selected = useBuilder((state) => state.selection.nodes);
  const nodeId = selected.length === 1 ? (selected[0] as string) : null;

  const nodeType = useBuilder((state) => (nodeId ? state.nodeType[nodeId] : undefined));
  const config = useBuilder((state) => (nodeId ? state.config[nodeId] : undefined));
  const issues = useBuilder((state) => (nodeId ? state.validation.byNode[nodeId] : undefined));
  const picklists = useBuilder((state) => state.picklists);
  const env = useBuilder((state) => state.env);
  const [tab, setTab] = useState<"config" | "problems">("config");

  const context = useMemo<FieldContextValue | null>(() => {
    if (!nodeId || !nodeType) {
      return null;
    }
    return {
      nodeId,
      nodeType,
      readOnly: !env.canEdit,
      picklists,
      issues: issues ?? [],
      env,
      set: (path: ConfigPath, value: unknown, historyKey?: string) =>
        value === undefined
          ? store.getState().clearConfig(nodeId, path)
          : store.getState().updateConfig(nodeId, path, value, historyKey),
      clear: (path: ConfigPath) => store.getState().clearConfig(nodeId, path),
    };
  }, [nodeId, nodeType, env, picklists, issues, store]);

  if (selected.length > 1) {
    return (
      <aside className="fb-inspector">
        <div className="fb-toolbar">
          <span className="text-xs font-semibold">{selected.length} nodes selected</span>
        </div>
        <div className="fb-inspector-body">
          <p className="fb-empty">Select a single node to configure it.</p>
          {env.canEdit ? (
            <button
              type="button"
              className="btn-outline-sm mt-2"
              onClick={() => store.getState().deleteNodes(selected)}
            >
              Delete {selected.length} nodes
            </button>
          ) : null}
        </div>
      </aside>
    );
  }

  if (!nodeId || !nodeType || !context) {
    return (
      <aside className="fb-inspector">
        <div className="fb-inspector-body">
          <p className="fb-empty">Select a node to configure it.</p>
        </div>
      </aside>
    );
  }

  const spec = nodeSpec(nodeType);
  const severity = worstSeverity(issues);

  return (
    <aside className="fb-inspector">
      <div className="fb-toolbar">
        <span className="text-xs font-semibold truncate">{spec?.label ?? nodeType}</span>
        <div className="ml-auto flex gap-1">
          <button
            type="button"
            className={tab === "config" ? "btn-outline-sm" : "btn-link text-xs"}
            onClick={() => setTab("config")}
          >
            Config
          </button>
          <button
            type="button"
            className={tab === "problems" ? "btn-outline-sm" : "btn-link text-xs"}
            onClick={() => setTab("problems")}
          >
            Problems
            {severity ? <span className={`fb-badge fb-badge-${severity} ml-1`}>{issues?.length}</span> : null}
          </button>
        </div>
      </div>

      <div className="fb-inspector-body">
        <FieldProvider value={context}>
          {tab === "config" ? (
            <SchemaField schema={configSchema(nodeType)} path={[]} value={config} propertyName="config" required />
          ) : (
            <ProblemList issues={issues ?? []} />
          )}
        </FieldProvider>
        {spec?.description ? <p className="fb-field-help mt-3">{spec.description}</p> : null}
      </div>
    </aside>
  );
}

function ProblemList({ issues }: { issues: readonly { code: string; message: string; severity: string }[] }) {
  if (issues.length === 0) {
    return <p className="fb-empty">Nothing wrong with this node.</p>;
  }
  return (
    <ul>
      {issues.map((issue, index) => (
        <li key={index} className={`fb-problem fb-problem-${issue.severity}`}>
          {issue.message}
          <span className="fb-problem-code block">{issue.code}</span>
        </li>
      ))}
    </ul>
  );
}
