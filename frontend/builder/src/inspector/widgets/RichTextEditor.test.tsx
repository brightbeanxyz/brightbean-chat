/**
 * The email body editor.
 *
 * jsdom does not implement `document.execCommand`, which is deliberate on their
 * part and useful here: the tests stub it, so what is asserted is the *wiring* —
 * which command each button issues, and that the value pushed upward is the
 * sanitized DOM — rather than a browser's own formatting behaviour, which is not
 * ours to test.
 *
 * The sanitizer is tested directly, because it is the half that has to agree
 * with the server's allowlist in apps/channels/providers/email_html.py.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FieldProvider, type FieldContextValue } from "../FieldContext";
import { RichTextEditor, sanitizeHtml } from "./RichTextEditor";

interface Written {
  path: (string | number)[];
  value: unknown;
}

function renderEditor(value: string, overrides: Partial<FieldContextValue> = {}) {
  const written: Written[] = [];
  const context = {
    nodeId: "n1",
    nodeType: "send_email",
    readOnly: false,
    picklists: { tags: [], custom_fields: [{ id: "plan", label: "Plan" }], flows: [], members: [], sequences: [] },
    issues: [],
    env: {} as FieldContextValue["env"],
    set: (path: (string | number)[], next: unknown) => written.push({ path, value: next }),
    clear: () => undefined,
    ...overrides,
  } as unknown as FieldContextValue;

  render(
    <FieldProvider value={context}>
      <RichTextEditor
        schema={{ type: "string", maxLength: 100000 }}
        path={["html_body"]}
        value={value}
        propertyName="html_body"
        required
      />
    </FieldProvider>,
  );
  return written;
}

describe("sanitizeHtml", () => {
  it("keeps the allowlisted tags", () => {
    expect(sanitizeHtml("<p>Hello <strong>there</strong></p>")).toBe("<p>Hello <strong>there</strong></p>");
  });

  it("drops a script outright rather than unwrapping it", () => {
    // Unwrapping would paste the script *body* into the message as text.
    expect(sanitizeHtml("<p>a</p><script>alert(1)</script>")).toBe("<p>a</p>");
  });

  it("unwraps an unknown tag but keeps its words", () => {
    expect(sanitizeHtml("<section><p>Kept</p></section>")).toBe("<p>Kept</p>");
  });

  it("removes event handlers", () => {
    expect(sanitizeHtml('<p onclick="steal()">x</p>')).toBe("<p>x</p>");
  });

  it("removes a javascript: href", () => {
    expect(sanitizeHtml('<a href="javascript:alert(1)">x</a>')).toBe("<a>x</a>");
  });

  it("keeps an http href and a mailto", () => {
    expect(sanitizeHtml('<a href="https://x.test/a">x</a>')).toBe('<a href="https://x.test/a">x</a>');
    expect(sanitizeHtml('<a href="mailto:a@b.test">x</a>')).toBe('<a href="mailto:a@b.test">x</a>');
  });

  it("removes attributes that are not on the tag's allowlist", () => {
    expect(sanitizeHtml('<p style="color:red" id="x">a</p>')).toBe("<p>a</p>");
  });

  it("keeps an image with an https src", () => {
    expect(sanitizeHtml('<img src="https://cdn.test/a.png" alt="A" />')).toBe(
      '<img src="https://cdn.test/a.png" alt="A">',
    );
  });
});

describe("RichTextEditor", () => {
  beforeEach(() => {
    (document as unknown as { execCommand: unknown }).execCommand = vi.fn(() => true);
  });

  it("renders the value into the editable surface", () => {
    renderEditor("<p>Hello</p>");
    expect(screen.getByRole("textbox", { name: "Email body" }).innerHTML).toBe("<p>Hello</p>");
  });

  it("issues the right command for each toolbar button", () => {
    renderEditor("<p>a</p>");
    const exec = document.execCommand as unknown as ReturnType<typeof vi.fn>;

    fireEvent.mouseDown(screen.getByRole("button", { name: "Bold" }));
    expect(exec).toHaveBeenCalledWith("bold", false, undefined);

    fireEvent.mouseDown(screen.getByRole("button", { name: "Bulleted list" }));
    expect(exec).toHaveBeenCalledWith("insertUnorderedList", false, undefined);

    fireEvent.mouseDown(screen.getByRole("button", { name: "Heading" }));
    expect(exec).toHaveBeenCalledWith("formatBlock", false, "<h2>");
  });

  it("does not throw when execCommand is missing", () => {
    // An older engine, or a test environment. The panel must survive it.
    delete (document as unknown as { execCommand?: unknown }).execCommand;
    renderEditor("<p>a</p>");
    expect(() => fireEvent.mouseDown(screen.getByRole("button", { name: "Bold" }))).not.toThrow();
  });

  it("pushes the edited value upward on input", () => {
    const written = renderEditor("<p>a</p>");
    const surface = screen.getByRole("textbox", { name: "Email body" });
    surface.innerHTML = "<p>ab</p>";
    fireEvent.input(surface);
    expect(written.at(-1)?.value).toBe("<p>ab</p>");
  });

  it("sanitizes on blur", () => {
    const written = renderEditor("<p>a</p>");
    const surface = screen.getByRole("textbox", { name: "Email body" });
    surface.innerHTML = '<p onclick="x()">a</p><script>alert(1)</script>';
    fireEvent.blur(surface);
    expect(written.at(-1)?.value).toBe("<p>a</p>");
  });

  it("sanitizes pasted markup", () => {
    // Paste is the one path by which markup from anywhere at all enters the
    // document, so it is intercepted rather than trusted and cleaned later.
    const written = renderEditor("");
    const surface = screen.getByRole("textbox", { name: "Email body" });
    const exec = document.execCommand as unknown as ReturnType<typeof vi.fn>;

    fireEvent.paste(surface, {
      clipboardData: { getData: (type: string) => (type === "text/html" ? '<p onclick="x">Hi</p>' : "") },
    });

    expect(exec).toHaveBeenCalledWith("insertHTML", false, "<p>Hi</p>");
    expect(written.at(-1)?.value).not.toContain("onclick");
  });

  it("escapes a plain-text paste", () => {
    renderEditor("");
    const surface = screen.getByRole("textbox", { name: "Email body" });
    const exec = document.execCommand as unknown as ReturnType<typeof vi.fn>;

    fireEvent.paste(surface, {
      clipboardData: { getData: (type: string) => (type === "text/html" ? "" : "<b>not markup</b>") },
    });

    expect(exec).toHaveBeenCalledWith("insertHTML", false, "&lt;b&gt;not markup&lt;/b&gt;");
  });

  it("offers the placeholder tokens, including custom fields", () => {
    renderEditor("<p>a</p>");
    fireEvent.click(screen.getByRole("button", { name: "Insert a contact field" }));
    expect(screen.getByRole("option", { name: "{{first_name}}" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "{{plan}}" })).toBeTruthy();
  });

  it("inserts a token as literal text", () => {
    renderEditor("<p>a</p>");
    const exec = document.execCommand as unknown as ReturnType<typeof vi.fn>;
    fireEvent.click(screen.getByRole("button", { name: "Insert a contact field" }));
    fireEvent.mouseDown(screen.getByRole("option", { name: "{{first_name}}" }));
    expect(exec).toHaveBeenCalledWith("insertHTML", false, "{{first_name}}");
  });

  it("swaps to a raw-HTML textarea and back", () => {
    renderEditor("<p>Hello</p>");
    fireEvent.click(screen.getByRole("button", { name: "Edit the HTML directly" }));
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.tagName).toBe("TEXTAREA");
    expect(textarea.value).toBe("<p>Hello</p>");
  });

  it("still shows the body after coming back from the source view", () => {
    // Leaving the source view mounts a FRESH contenteditable, which shows
    // nothing whatever the value says. Comparing values alone concluded it was
    // already showing the body, so the editor came back blank — and the next
    // keystroke would then push that emptiness over the real value.
    renderEditor("<p>Hello</p>");
    const toggle = screen.getByRole("button", { name: "Edit the HTML directly" });
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    expect(screen.getByRole("textbox", { name: "Email body" }).innerHTML).toBe("<p>Hello</p>");
  });

  it("does not report the empty surface as an edit after a source round trip", () => {
    const written = renderEditor("<p>Hello</p>");
    const toggle = screen.getByRole("button", { name: "Edit the HTML directly" });
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    fireEvent.input(screen.getByRole("textbox", { name: "Email body" }));
    expect(written.at(-1)?.value).toBe("<p>Hello</p>");
  });

  it("writes what the source view types", () => {
    const written = renderEditor("<p>Hello</p>");
    fireEvent.click(screen.getByRole("button", { name: "Edit the HTML directly" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "<p>Edited</p>" } });
    expect(written.at(-1)?.value).toBe("<p>Edited</p>");
  });

  it("is not editable when the canvas is read-only", () => {
    renderEditor("<p>a</p>", { readOnly: true });
    const surface = screen.getByRole("textbox", { name: "Email body" });
    expect(surface.getAttribute("contenteditable")).toBe("false");
    expect(screen.getByRole("button", { name: "Bold" })).toHaveProperty("disabled", true);
  });

  it("does not run a command when read-only", () => {
    renderEditor("<p>a</p>", { readOnly: true });
    const exec = document.execCommand as unknown as ReturnType<typeof vi.fn>;
    fireEvent.mouseDown(screen.getByRole("button", { name: "Bold" }));
    expect(exec).not.toHaveBeenCalled();
  });
});
