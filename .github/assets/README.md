# README assets

Images the top-level README embeds. They live here rather than in `static/`
because the app never serves them, and `.dockerignore` keeps this folder out of
the image.

- `channels/` holds the channel badges, copied unchanged from brightbean-website's
  `src/assets/img/logos/` so the README and the site show the same marks. That
  folder's README has the provenance: Simple Icons (CC0) and the postiz badge
  set on brand-colour badges, and neutral house badges for SMS and email, which
  are not brands. To update one, copy the new file over from there and keep the
  name.
- `icons/` holds [Lucide](https://lucide.dev) icons (ISC) from
  `lucide-static@1.49.0`, with `currentColor` replaced by `#EA580C`
  (`--brand-600`). Inside an `<img>`, `currentColor` resolves to black, which
  disappears on GitHub's dark theme.

The channel marks are the trademarks of their respective owners and appear here
only to identify the channels BrightBean Chat connects.
