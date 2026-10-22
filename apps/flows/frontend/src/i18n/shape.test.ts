import { describe, expect, it } from "vitest";

import { en } from "./en";
import { ky } from "./ky";
import { ru } from "./ru";

/** Every leaf's dotted path, e.g. "publishState.save.clean". */
function leafPaths(node: unknown, prefix = ""): string[] {
  if (typeof node !== "object" || node === null) {
    return [prefix];
  }
  return Object.entries(node).flatMap(([key, value]) => leafPaths(value, prefix ? `${prefix}.${key}` : key));
}

/**
 * A leaf's plural *family* — its path with any CLDR plural suffix stripped.
 *
 * Russian's four categories and Kyrgyz's one both differ from English's two,
 * so `ru.ts`/`ky.ts` legitimately define a different set of `_one`/`_few`/
 * `_many`/`_other` leaves than `en.ts` for the same counted phrase. What has
 * to match across all three is the family — that `toolbar.toFix` exists in
 * each — not which suffixes it is spelled with.
 */
function family(path: string): string {
  return path.replace(/_(zero|one|two|few|many|other)$/, "");
}

describe("ru.ts and ky.ts mirror en.ts", () => {
  const enPaths = leafPaths(en).sort();
  const enFamilies = new Set(enPaths.map(family));

  for (const [name, locale] of [
    ["ru", ru],
    ["ky", ky],
  ] as const) {
    it(`${name}.ts covers exactly en.ts's keys (by plural family), none missing or extra`, () => {
      const localeFamilies = new Set(leafPaths(locale).map(family));
      expect([...localeFamilies].sort()).toEqual([...enFamilies].sort());
    });

    it(`${name}.ts leaves no value untranslated (equal to the English text)`, () => {
      // Symbols, HTTP-style tokens and a few deliberately-empty strings carry
      // over unchanged by design (see en.ts's `sample.placeholder` comments),
      // so this only flags a leaf whose text looks like English prose.
      const carriedOverByDesign = new Set([
        // Single-letter command abbreviations — near-universal, not prose.
        "richText.commands.bold.label",
        "richText.commands.italic.label",
        "richText.commands.underline.label",
        "richText.commands.heading.label",
        // Sample URLs and technical placeholder tokens, not prose.
        "sample.placeholder.url",
        "sample.placeholder.media_url",
        "sample.placeholder.image",
        "sample.placeholder.name",
        "sample.placeholder.json_path",
        "mediaBlock.urlPlaceholder",
        // Brand names, never translated.
        "inspector.enumLabels.instagram",
        "inspector.enumLabels.messenger",
        "inspector.enumLabels.whatsapp",
        "inspector.enumLabels.telegram",
        "inspector.enumLabels.sms",
        "inspector.labels.url",
        "inspector.labels.id",
        // A JSON-Schema path pattern example, not prose.
        "inspector.help.slot",
      ]);
      const untranslated: string[] = [];
      for (const path of enPaths) {
        if (carriedOverByDesign.has(path)) {
          continue;
        }
        const english = path.split(".").reduce<unknown>((acc, key) => (acc as Record<string, unknown>)?.[key], en);
        const translated = path
          .split(".")
          .reduce<unknown>((acc, key) => (acc as Record<string, unknown>)?.[key], locale);
        if (
          typeof english === "string" &&
          typeof translated === "string" &&
          english === translated &&
          /[A-Za-z]{2}/.test(english)
        ) {
          untranslated.push(path);
        }
      }
      expect(untranslated).toEqual([]);
    });
  }
});
