# Lorekeeper website

The public landing page. It is a static site, separate from the self-hosted app: the app serves the
Library, Reader and Runs, and this folder is what a project homepage hosts (GitHub Pages, Netlify,
any static host).

- `index.html`, `site.css`, `site.js`: the page. It reuses the app's design tokens (`css/base.css`)
  so it stays the same "lit and unlit" system as the product.
- `demo-data.js`: the Dracula index (Project Gutenberg #345, built with Claude Haiku) flattened for
  the hero demo: story-so-far recaps, events, and facets without the profile paragraphs. The demo
  gates it with the spec's rule (an item is visible when the ordinal of its `from` anchor is at or
  before the position). Rebuild it with `uv run python bench/site_demo_data.py INDEX.lore.json BOOK.epub`, for this or
  any other public-domain book.
- `assets/`: screenshots of the app at 1440 wide, dark theme, as WebP.

## Build and deploy

```sh
uv run lorekeeper docs export build/      # landing page + docs + spec + styles + fonts, ready to host
```

The export lays out `index.html` at the root next to `docs/`, `css/`, `fonts/`, `vendor/`, `spec.md`
and `readme.md`, so the page's relative links resolve. Serve the folder over HTTP (the page is an ES
module, so `file://` won't do): `python3 -m http.server -d build 8790`.

## Updating screenshots

Run the app, open the page you want at 1440 by 900 in the dark theme, screenshot it, and convert:

```sh
cwebp -q 82 shot.png -o site/assets/<name>.webp
```

Keep the `width`/`height` attributes on the `<img>` tags at 1440 by 900 so the layout reserves space.
