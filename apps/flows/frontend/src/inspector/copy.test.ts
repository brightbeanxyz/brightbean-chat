/**
 * `helpFor`'s guard: only translate a field that actually has help text.
 *
 * The ordering here is load-bearing, not incidental. A property name like
 * `field` carries a description on one node type and none on another
 * (apps/flows/schema/nodes.py) — checking the translation table before the
 * schema's own `description` would show help text under a field the schema
 * never gave one to, sourced from a *different* field that happens to share
 * its property name. These tests are what catches that regression if
 * `helpFor` is ever "simplified" back to `lookup(...) ?? description`.
 */
import { describe, expect, it } from "vitest";

import i18n from "../i18n";
import { helpFor } from "./copy";

describe("helpFor", () => {
  it("renders nothing for a field the schema gave no description to, even when the property name has a translation elsewhere", () => {
    // "field" has a translated help entry (used by smart_delay_date's date
    // field), but set_field's "field" carries no schema description at all.
    expect(helpFor("field", undefined)).toBeUndefined();
  });

  it("falls back to the schema's own English text when no translation is registered", () => {
    expect(helpFor("something_new", "A description nobody has translated yet.")).toBe(
      "A description nobody has translated yet.",
    );
  });

  it("translates a described field under the active language", async () => {
    await i18n.changeLanguage("ru");
    try {
      expect(helpFor("weight", "Percent.")).toBe("Проценты.");
    } finally {
      await i18n.changeLanguage("en");
    }
  });
});
