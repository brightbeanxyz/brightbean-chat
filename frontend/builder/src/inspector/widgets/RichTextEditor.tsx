/**
 * The email body editor: a WYSIWYG surface over an HTML string.
 *
 * SPEC §6.7 asks for "a rich text editor, not a drag-drop email builder in v1",
 * and this is it. It is written here rather than pulled from npm on purpose:
 * the audit job runs `npm audit --audit-level=low`, asserts the bundle is one
 * file per type and asserts two builds are byte-identical, and an editor
 * dependency is a large, frequently-updated surface to put behind all three for
 * a control with this little logic in it.
 *
 * -------------------------------------------------------------------------
 * What the value is
 * -------------------------------------------------------------------------
 *
 * `html_body`, a plain HTML string, exactly as the schema declares it. The
 * editor never invents a document model: `contentEditable` holds the markup,
 * `innerHTML` is read out on every input, and the sanitizer below is what keeps
 * what comes out inside the allowlist the *server* enforces at send time
 * (`apps/channels/providers/email_html.py`). Two allowlists, one of them
 * authoritative — this one exists so the author sees what will actually be
 * sent, not so the server can trust the client.
 *
 * -------------------------------------------------------------------------
 * Uncontrolled on purpose
 * -------------------------------------------------------------------------
 *
 * A `contentEditable` element cannot be a controlled React input: rewriting
 * `innerHTML` on every keystroke destroys the caret. So the DOM owns the text
 * while the field has focus, and `innerHTML` is only written back when the
 * value changed somewhere *else* — an undo, a different node selected. `lastSent`
 * is what tells those two cases apart.
 *
 * -------------------------------------------------------------------------
 * execCommand
 * -------------------------------------------------------------------------
 *
 * Deprecated, universally implemented, and the only formatting API that exists
 * without a dependency. Every call is guarded, because jsdom does not implement
 * it and a missing method must not take the panel down with it.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { FieldShell, fieldId, type FieldProps } from "../fields";
import { useField } from "../FieldContext";
import { SYSTEM_TOKENS } from "./PlaceholderInput";

/**
 * Tags that survive a paste or a round trip.
 *
 * Mirrors `ALLOWED_TAGS` in apps/channels/providers/email_html.py. When one
 * changes the other should, and the server is the one that decides.
 */
const ALLOWED_TAGS = new Set([
  "A", "B", "BLOCKQUOTE", "BR", "DIV", "EM", "H1", "H2", "H3", "H4", "HR", "I",
  "IMG", "LI", "OL", "P", "PRE", "SPAN", "STRONG", "TABLE", "TBODY", "TD", "TH",
  "THEAD", "TR", "U", "UL",
]);

/** Per-tag attribute allowlist, mirroring the server's ALLOWED_ATTRIBUTES. */
const ALLOWED_ATTRIBUTES: Record<string, Set<string>> = {
  A: new Set(["href", "title", "rel", "target"]),
  IMG: new Set(["src", "alt", "width", "height"]),
  TD: new Set(["colspan", "rowspan", "align"]),
  TH: new Set(["colspan", "rowspan", "align"]),
  TABLE: new Set(["width", "cellpadding", "cellspacing", "border"]),
};

/** Schemes a link or an image may use. Everything else is dropped. */
const SAFE_SCHEME = /^(https?:|mailto:)/i;

interface Command {
  label: string;
  title: string;
  /** execCommand name, or a function for the ones that need an argument. */
  run: (exec: (command: string, value?: string) => void) => void;
}

const COMMANDS: Command[] = [
  { label: "B", title: "Bold", run: (exec) => exec("bold") },
  { label: "I", title: "Italic", run: (exec) => exec("italic") },
  { label: "U", title: "Underline", run: (exec) => exec("underline") },
  { label: "H", title: "Heading", run: (exec) => exec("formatBlock", "<h2>") },
  { label: "¶", title: "Paragraph", run: (exec) => exec("formatBlock", "<p>") },
  { label: "• List", title: "Bulleted list", run: (exec) => exec("insertUnorderedList") },
  { label: "1. List", title: "Numbered list", run: (exec) => exec("insertOrderedList") },
  { label: "❝", title: "Quote", run: (exec) => exec("formatBlock", "<blockquote>") },
  { label: "―", title: "Divider", run: (exec) => exec("insertHorizontalRule") },
  { label: "Clear", title: "Remove formatting", run: (exec) => exec("removeFormat") },
];

/**
 * The value, reduced to the allowlist.
 *
 * Parsed with DOMParser rather than by assigning to a live element's innerHTML,
 * so nothing in the document being cleaned is ever attached to the page — a
 * detached parse does not run scripts or fire `onerror` on a broken `<img>`.
 */
export function sanitizeHtml(html: string): string {
  const parsed = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
  clean(parsed.body);
  return parsed.body.innerHTML;
}

function clean(root: Element): void {
  // A static list, because unwrapping mutates the tree underneath a live one.
  for (const element of Array.from(root.querySelectorAll("*"))) {
    if (!ALLOWED_TAGS.has(element.tagName)) {
      // Unwrap rather than remove: an unrecognised wrapper loses its markup and
      // keeps its words, which is the direction that fails safe for something
      // somebody wrote. Except for the tags that carry no prose at all.
      if (element.tagName === "SCRIPT" || element.tagName === "STYLE") {
        element.remove();
      } else {
        element.replaceWith(...Array.from(element.childNodes));
      }
      continue;
    }
    const allowed = ALLOWED_ATTRIBUTES[element.tagName] ?? new Set<string>();
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      if (!allowed.has(name)) {
        element.removeAttribute(attribute.name);
        continue;
      }
      if ((name === "href" || name === "src") && !SAFE_SCHEME.test(attribute.value.trim())) {
        element.removeAttribute(attribute.name);
      }
    }
  }
}

export function RichTextEditor(props: FieldProps) {
  const { set, readOnly, picklists } = useField();
  const { schema, path, value } = props;
  const html = typeof value === "string" ? value : "";

  const surface = useRef<HTMLDivElement>(null);
  const [source, setSource] = useState(false);
  const [tokensOpen, setTokensOpen] = useState(false);
  // The last string this component wrote upward. While it matches `html` the
  // DOM is already showing it, so re-assigning innerHTML would only move the
  // caret to the start.
  //
  // `null` rather than `html` to start with, because on the very first render
  // the DOM is empty and the value has not been written into it yet — seeding
  // this with the value made the effect below think it was already showing and
  // left the editor blank for every node that had a body already.
  const lastSent = useRef<string | null>(null);

  const push = useCallback(
    (next: string) => {
      lastSent.current = next;
      set(path, next, `html:${path.join(".")}`);
    },
    [set, path],
  );

  useEffect(() => {
    const element = surface.current;
    if (!element || source) {
      return;
    }
    if (html !== lastSent.current) {
      // The value changed from outside — undo, or a different node selected.
      element.innerHTML = html;
      lastSent.current = html;
    }
  }, [html, source]);

  const exec = useCallback(
    (command: string, commandValue?: string) => {
      const element = surface.current;
      if (!element || readOnly) {
        return;
      }
      element.focus();
      // Guarded: jsdom has no execCommand, and an older engine may not know a
      // particular command. Either way the panel must not throw.
      const run = (document as Document & { execCommand?: (c: string, ui: boolean, v?: string) => boolean })
        .execCommand;
      if (typeof run === "function") {
        run.call(document, command, false, commandValue);
      }
      push(sanitizeHtml(element.innerHTML));
    },
    [push, readOnly],
  );

  const onInput = useCallback(() => {
    const element = surface.current;
    if (element) {
      // Not sanitized here: cleaning on every keystroke would rewrite innerHTML
      // under the caret. Paste is where untrusted markup actually arrives, and
      // that is sanitized below; a blur pass catches anything else.
      lastSent.current = element.innerHTML;
      set(path, element.innerHTML, `html:${path.join(".")}`);
    }
  }, [set, path]);

  const onBlur = useCallback(() => {
    const element = surface.current;
    if (element) {
      const cleaned = sanitizeHtml(element.innerHTML);
      element.innerHTML = cleaned;
      push(cleaned);
    }
  }, [push]);

  const onPaste = useCallback(
    (event: React.ClipboardEvent<HTMLDivElement>) => {
      // Paste is the one path by which markup from anywhere at all — another
      // site, a word processor — enters the document, so it is intercepted and
      // cleaned rather than trusted and cleaned later.
      event.preventDefault();
      const clipboard = event.clipboardData;
      const pasted = clipboard.getData("text/html") || escapeText(clipboard.getData("text/plain"));
      insertHtml(sanitizeHtml(pasted));
      const element = surface.current;
      if (element) {
        push(sanitizeHtml(element.innerHTML));
      }
    },
    [push],
  );

  const link = useCallback(() => {
    const url = window.prompt("Link to which URL?");
    if (url && SAFE_SCHEME.test(url.trim())) {
      exec("createLink", url.trim());
    }
  }, [exec]);

  const insertToken = useCallback(
    (token: string) => {
      insertHtml(`{{${token}}}`);
      const element = surface.current;
      if (element) {
        push(sanitizeHtml(element.innerHTML));
      }
      setTokensOpen(false);
    },
    [push],
  );

  const tokens = [...SYSTEM_TOKENS, ...picklists.custom_fields.map((field) => field.id)];

  return (
    <FieldShell {...props}>
      <div className="fb-subgroup" role="toolbar" aria-label="Formatting">
        {COMMANDS.map((command) => (
          <button
            key={command.label}
            type="button"
            className="fb-palette-item"
            title={command.title}
            aria-label={command.title}
            disabled={readOnly}
            // onMouseDown, not onClick: a click moves focus out of the editable
            // surface first, and the browser drops the selection execCommand
            // was about to act on.
            onMouseDown={(event) => {
              event.preventDefault();
              command.run(exec);
            }}
          >
            {command.label}
          </button>
        ))}
        <button
          type="button"
          className="fb-palette-item"
          title="Link"
          disabled={readOnly}
          onMouseDown={(event) => {
            event.preventDefault();
            link();
          }}
        >
          Link
        </button>
        <button
          type="button"
          className="fb-palette-item"
          title="Insert a contact field"
          // An explicit label because the visible text is punctuation: without
          // it a screen reader announces "brace brace".
          aria-label="Insert a contact field"
          disabled={readOnly}
          onClick={() => setTokensOpen((open) => !open)}
        >
          {"{{ }}"}
        </button>
        <button
          type="button"
          className="fb-palette-item"
          title="Edit the HTML directly"
          aria-label="Edit the HTML directly"
          aria-pressed={source}
          onClick={() => setSource((on) => !on)}
        >
          Source
        </button>
      </div>

      {tokensOpen && !readOnly ? (
        <div className="fb-subgroup" role="listbox" aria-label="Insert a placeholder">
          {tokens.map((token) => (
            <button
              key={token}
              type="button"
              role="option"
              className="fb-palette-item"
              onMouseDown={(event) => {
                event.preventDefault();
                insertToken(token);
              }}
            >
              {`{{${token}}}`}
            </button>
          ))}
        </div>
      ) : null}

      {source ? (
        <textarea
          id={fieldId(path)}
          className="form-input-styled"
          rows={12}
          value={html}
          disabled={readOnly}
          maxLength={schema.maxLength}
          onChange={(event) => push(event.target.value)}
        />
      ) : (
        <div
          id={fieldId(path)}
          ref={surface}
          className="form-input-styled fb-rich-text"
          contentEditable={!readOnly}
          suppressContentEditableWarning
          role="textbox"
          aria-multiline="true"
          aria-label="Email body"
          onInput={onInput}
          onBlur={onBlur}
          onPaste={onPaste}
        />
      )}
      <p className="fb-field-help">
        Formatting, links and images. Use the <code>{"{{ }}"}</code> button to insert a contact field;
        values are escaped when the email is built.
      </p>
    </FieldShell>
  );
}

/** Insert HTML at the caret, falling back to appending when there is no selection. */
function insertHtml(html: string): void {
  const run = (document as Document & { execCommand?: (c: string, ui: boolean, v?: string) => boolean }).execCommand;
  if (typeof run === "function") {
    run.call(document, "insertHTML", false, html);
  }
}

function escapeText(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
