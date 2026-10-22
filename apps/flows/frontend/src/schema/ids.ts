/**
 * Id minting for nodes, edges and the config items that back dynamic handles.
 *
 * Every id has to match apps/flows/schema/envelope.py::ID_PATTERN
 * (`^[A-Za-z0-9_-]{1,64}$`) — these reach idempotency keys and sticky-randomizer
 * variable names, so they are an allowlist rather than "any string".
 *
 * Short on purpose: a 100-node graph carries several hundred of them, and they
 * all count against the 512 KiB graph cap.
 */

const ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

function token(length: number): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  let out = "";
  for (const byte of bytes) {
    out += ALPHABET[byte % ALPHABET.length];
  }
  return out;
}

export function newNodeId(): string {
  return `n_${token(10)}`;
}

export function newEdgeId(): string {
  return `e_${token(10)}`;
}

/** For buttons, quick replies and randomizer paths — anything with an `id`. */
export function newItemId(): string {
  return token(8);
}
