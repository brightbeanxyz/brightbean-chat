/**
 * The island's translations, initialised once at module load with every
 * locale's resources already in hand — there is no backend plugin and no
 * lazy chunk to await, matching vite.config.mts's `inlineDynamicImports`
 * bundle. `i18next.init()` is therefore synchronous whenever resources are
 * passed directly (see the library's own docs), so a `useTranslation()` call
 * right after import never race the resources it reads.
 *
 * The active language is Django's, not the browser's: `setBuilderLocale`
 * reads the mount div's `data-locale` (see ../env.ts), so the shell and the
 * island can never disagree about which language a session is in. There is
 * deliberately no `i18next-browser-languagedetector` — the locale already
 * has one true source, and detecting it a second way independently is the
 * two-conventions problem the rest of this rollout avoids.
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import { en } from "./en";
import { ky } from "./ky";
import { ru } from "./ru";

export const SUPPORTED_LOCALES = ["en", "ru", "ky"] as const;
export type BuilderLocale = (typeof SUPPORTED_LOCALES)[number];

function isSupportedLocale(value: string): value is BuilderLocale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    ru: { translation: ru },
    ky: { translation: ky },
  },
  lng: "en",
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes; double-escaping breaks apostrophes in interpolated names.
  returnNull: false,
});

/** Switch the island's language. Falls back to English for anything unrecognised rather than throwing. */
export function setBuilderLocale(locale: string): void {
  void i18n.changeLanguage(isSupportedLocale(locale) ? locale : "en");
}

export default i18n;
