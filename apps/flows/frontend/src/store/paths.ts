/**
 * Reading and writing a value inside an opaque config, by path.
 *
 * The config is whatever JSON the schema allows, so the form addresses it with
 * a path of string keys and numeric indexes. Updates are immutable and shallow:
 * only the objects on the path are copied, which is what makes a history
 * snapshot cost O(depth) rather than O(graph).
 *
 * `unsetIn` deletes the key rather than writing `""` or `null`. That distinction
 * is load-bearing: most optional strings in the schema carry `minLength: 1`, and
 * every object is closed, so "cleared" has to mean absent.
 */

export type PathStep = string | number;
export type ConfigPath = readonly PathStep[];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function getIn(value: unknown, path: ConfigPath): unknown {
  let current = value;
  for (const step of path) {
    if (typeof step === "number") {
      if (!Array.isArray(current)) {
        return undefined;
      }
      current = current[step];
    } else {
      if (!isRecord(current)) {
        return undefined;
      }
      current = current[step];
    }
  }
  return current;
}

function cloneStep(container: unknown, step: PathStep): unknown {
  if (typeof step === "number") {
    return Array.isArray(container) ? [...container] : [];
  }
  return isRecord(container) ? { ...container } : {};
}

export function setIn(root: unknown, path: ConfigPath, value: unknown): unknown {
  if (path.length === 0) {
    return value;
  }
  const [step, ...rest] = path as [PathStep, ...PathStep[]];
  const copy = cloneStep(root, step) as Record<PathStep, unknown>;
  const child = Array.isArray(copy) ? copy[step as number] : copy[step];
  copy[step] = rest.length === 0 ? value : setIn(child, rest, value);
  return copy;
}

export function unsetIn(root: unknown, path: ConfigPath): unknown {
  if (path.length === 0) {
    return undefined;
  }
  const [step, ...rest] = path as [PathStep, ...PathStep[]];

  if (rest.length === 0) {
    if (typeof step === "number") {
      if (!Array.isArray(root)) {
        return root;
      }
      return root.filter((_unused, index) => index !== step);
    }
    if (!isRecord(root)) {
      return root;
    }
    const copy = { ...root };
    delete copy[step];
    return copy;
  }

  const child = getIn(root, [step]);
  if (child === undefined) {
    return root;
  }
  return setIn(root, [step], unsetIn(child, rest));
}

/** `blocks[0].text` — the shape the server's `path` field uses, for matching. */
export function formatPath(path: ConfigPath): string {
  let out = "";
  for (const step of path) {
    out += typeof step === "number" ? `[${step}]` : out === "" ? step : `.${step}`;
  }
  return out;
}
