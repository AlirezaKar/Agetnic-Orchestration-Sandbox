# Redesign reference (v0.12.0)

The user-supplied design that the 0.12.0 interface is built on. Kept here so the
look has a source of truth that outlives a chat attachment.

| File | What it is |
| --- | --- |
| `preview.png` | the acceptance target: how the page should look at 1440 px |
| `reference.html` | the concept prototype — markup, design tokens and component CSS |

`reference.html` is a **static prototype**, not the app: it loads `marked` and
`DOMPurify` from a CDN, answers from a hardcoded fake run and hooks a
`window.AGENT_API_CONFIG.send()` that does not exist. Treat it as the visual
spec only.

What the app took from it:

* `app/static/assets/css/styles.css` — ported verbatim (tokens + shell + component
  CSS + media queries + the "refinement pass" block that re-declares the tokens,
  so the refined values win).
* `app/static/index.html` — the same three-column shell (top bar with status
  pills, sidebar, chat column, inspector drawer) wired to the real API ids.
* `app/static/assets/js/app.js` — the same behaviour, but with a **local**
  Markdown renderer instead of the CDN pair: headings, tables, lists, callouts,
  fenced code and inline code/bold/italic/links are parsed into DOM nodes, so a
  conversation still reads correctly offline and model output can never inject
  markup.
* `app/static/assets/css/extra.css` — everything app-specific (transcript
  orchestration panel, attachments/permission card, run/session/agent lists,
  diagnostics rows, toasts) built on the design tokens.

Never copied from the prototype: the fake run data, the fake composer target and
the CDN dependency.
