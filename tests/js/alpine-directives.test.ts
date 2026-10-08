/**
 * Every Alpine directive in every template compiles the way Alpine compiles it.
 *
 * Alpine does not run a directive value as a script. It splices it into
 * `__self.result = <value>` inside an AsyncFunction, so the value has to be an
 * expression — with two carve-outs, values starting with `if (` or `let`/`const`,
 * which it wraps in an async arrow first. Anything else that is a statement is a
 * SyntaxError, and Alpine reports it to the console and carries on: the page
 * renders, nothing throws during a Django test, and the directive is simply dead.
 *
 * That is not hypothetical. templates/base.html once carried
 * `x-effect="try { localStorage.setItem(...) } catch (e) {}"`, every app-shell
 * page logged `Unexpected token 'try'`, and the sidebar's collapsed state was
 * never written — while apps/common/tests/test_shell.py, which asserted that the
 * setItem call was *in the page*, stayed green. A string in the page is not a
 * running effect, so this compiles each value instead.
 *
 * Django template syntax is resolved crudely and on purpose: `{{ … }}`,
 * `{% url %}` and `{% static %}` become an identifier, which is valid both bare
 * and inside a string literal, and an `{% if %}…{% else %}…{% endif %}` is
 * compiled once per branch. A value that still carries template syntax after
 * that is reported rather than guessed at.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../..");
const TEMPLATES = join(REPO, "templates");

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

/**
 * Alpine 3.16.3's evaluator, minus the evaluation. Read from
 * `generateFunctionFromString` in the vendored build; the first test below
 * fails if that build stops containing the shape this copies.
 */
function compileLikeAlpine(expression: string): void {
  const wrapped =
    /^[\n\s]*if.*\(.*\)/.test(expression.trim()) || /^(let|const)\s/.test(expression.trim())
      ? `(async()=>{ ${expression} })()`
      : expression;
  new AsyncFunction(
    ["__self", "scope"],
    `with (scope) { __self.result = ${wrapped} }; __self.finished = true; return __self.result;`,
  );
}

/** Directives whose value is not JavaScript: a loop clause, a name, a selector, classes. */
const NOT_AN_EXPRESSION = /^x-(for|ref|teleport|transition|cloak|ignore)\b/;

const DIRECTIVE = /\s((?:x-[\w-]+|@|:)[\w:.-]*)=(?:"([^"]*)"|'([^']*)')/g;

function templates(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return templates(path);
    return entry.name.endsWith(".html") ? [path] : [];
  });
}

/** One variant per branch of a value's `{% if %}`s; nesting is left unresolved. */
function branches(value: string): string[] {
  if (!/{%\s*if\b/.test(value)) return [value];
  const ifBlock = /{%\s*if\b[^%]*%}([\s\S]*?)(?:{%\s*else\s*%}([\s\S]*?))?{%\s*endif\s*%}/g;
  return [value.replace(ifBlock, "$1"), value.replace(ifBlock, (_, _yes, no) => no ?? "")];
}

function unescapeAttribute(value: string): string {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#x?0*27;|&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

type Directive = { file: string; name: string; value: string };

function directives(): Directive[] {
  return templates(TEMPLATES).flatMap((path) => {
    const source = readFileSync(path, "utf8")
      // Prose about a directive is not a directive.
      .replace(/{%\s*comment\s*%}[\s\S]*?{%\s*endcomment\s*%}/g, "")
      .replace(/{#[\s\S]*?#}/g, "")
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<script\b[\s\S]*?<\/script>/g, "")
      // Before attribute extraction: `{{ x|default:"y" }}` carries a quote.
      .replace(/{{[\s\S]*?}}/g, "__django__")
      .replace(/{%\s*(?:url|static)\b[\s\S]*?%}/g, "__django__");
    const found: Directive[] = [];
    for (const [, name = "", doubleQuoted, singleQuoted = ""] of source.matchAll(DIRECTIVE)) {
      if (NOT_AN_EXPRESSION.test(name)) continue;
      found.push({ file: relative(REPO, path), name, value: unescapeAttribute(doubleQuoted ?? singleQuoted) });
    }
    return found;
  });
}

describe("Alpine directives", () => {
  it("are compiled here the way the vendored Alpine compiles them", () => {
    const alpine = readFileSync(join(REPO, "static/js/vendor/alpine.min.js"), "utf8");

    expect(alpine).toContain("with (scope) { __self.result = ${");
    expect(alpine).toContain("/^[\\n\\s]*if.*\\(.*\\)/.test(");
    expect(alpine).toContain("/^(let|const)\\s/.test(");
  });

  it("rejects a statement and accepts the forms Alpine wraps", () => {
    expect(() =>
      compileLikeAlpine("try { localStorage.setItem('sidebarCollapsed', sidebarCollapsed) } catch (e) {}"),
    ).toThrow(SyntaxError);
    expect(() => compileLikeAlpine("if (open) close()")).not.toThrow();
    expect(() => compileLikeAlpine("const x = 1; go(x)")).not.toThrow();
    expect(() => compileLikeAlpine("window.__bbStoreSidebarCollapsed(sidebarCollapsed)")).not.toThrow();
  });

  it("all compile", () => {
    const found = directives();
    // A scan that silently matched nothing would pass; the shell alone has dozens.
    expect(found.length).toBeGreaterThan(300);

    const failures: string[] = [];
    for (const { file, name, value } of found) {
      for (const variant of branches(value)) {
        if (variant.includes("{%")) {
          failures.push(`${file} ${name}: unresolved template syntax in ${JSON.stringify(variant)}`);
          continue;
        }
        try {
          compileLikeAlpine(variant);
        } catch (error) {
          failures.push(`${file} ${name}=${JSON.stringify(variant)}: ${(error as Error).message}`);
        }
      }
    }
    expect(failures).toEqual([]);
  });
});
