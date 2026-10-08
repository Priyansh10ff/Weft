# Weft: Design System

The interface is dark, quiet and precise, closer to an editor or a log viewer than a marketing site. Everything is neutral except two things: the five **thread colours**, one per modality, which mark where each piece of evidence came from, and **signal amber**, which marks the exact match: the second in a video, the region on a slide, the link that brought a result in.

Source of truth: `static/css/weft.css` (tokens and primitives), `static/css/app.css` (workspace), `static/css/landing.css` (landing page).

Related: [PRODUCT.md](./PRODUCT.md) · [TECHNICAL.md](./TECHNICAL.md)

---

## 1. Principles

- **Colour means provenance.** A thread colour always means a modality, on a chip, a dot, a thumbnail or an entity row. It is never used for decoration.
- **Amber means the match.** `signal` marks the exact place that answered the question (matched sentence, matched OCR region, exact timestamp), the link that brought a result in, the speaker, the active item and the focus ring. Nothing else is amber.
- **Evidence is the loudest thing on the page.** Labels are small uppercase mono. Chrome stays grey. The quote, the frame and the page are what the eye lands on.
- **Every number is mono.** Timestamps, page numbers, scores, counts and percentages use Geist Mono with tabular figures, so columns line up.
- **Flat and ruled.** Hairline borders separate things. Shadows only lift overlays (drawer, toasts). No gradients except the skeleton shimmer.
- **No filler.** No stock illustrations, no emoji. Empty states say what is missing and give one action.

## 2. Colour tokens

Defined as CSS custom properties on `:root` in `weft.css`. The page declares `color-scheme: dark`.

### Neutrals

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#0A0A09` | Page background |
| `--bg-raised` | `#0E0E0D` | Inputs, inset columns, sidebar |
| `--surface` | `#131311` | Cards, panels, table rows |
| `--surface-2` | `#191917` | Hover |
| `--surface-3` | `#21201D` | Pressed, selected chips, meter tracks |
| `--line` | `#262521` | Borders and dividers |
| `--line-2` | `#34332E` | Control borders |
| `--line-3` | `#4A4842` | Hovered borders, underlines |
| `--text` | `#ECE8DF` | Primary text |
| `--text-2` | `#BDB8AC` | Secondary text |
| `--muted` | `#8C877C` | Labels, meta |
| `--faint` | `#625E55` | Placeholders, missed items, disabled. Not for body text |
| `--primary` | `#ECE8DF` | Primary button fill |
| `--primary-ink` | `#0A0A09` | Text on the primary button |

### Signal and status

| Token | Hex | Use |
|---|---|---|
| `--signal` | `#E9B44C` | The exact match, links, speakers, active nav icon, focus ring |
| `--signal-soft` | `rgba(233, 180, 76, 0.12)` | Matched region fill, highlighted table row, focus glow |
| `--ok` | `#82C79D` | Success, high confidence, found evidence |
| `--warn` | `#E9B44C` | Medium confidence, proxy warnings |
| `--err` | `#E7685E` | Errors, low confidence, destructive hover |

### Thread colours (modalities)

| Token | Hex | Modality |
|---|---|---|
| `--m-video` | `#EA925D` | Video |
| `--m-audio` | `#82C79D` | Audio |
| `--m-image` | `#70B7DE` | Image |
| `--m-pdf` | `#DF7B71` | PDF |
| `--m-json` | `#B49EE0` | JSON and text |

A `.mod-{modality}` class sets `--c` to the thread colour, and components read `--c`, so one class colours a chip, a dot or a thumbnail glyph. Thread colours are never the only signal: chips also carry the modality name, and thumbnails carry a three-letter glyph (`VID`, `AUD`, `IMG`, `PDF`, `JSO`).

## 3. Type

| Role | Font | Notes |
|---|---|---|
| UI and body | Geist 400 / 500 / 600 | 15 px base, line height 1.55, stylistic sets `ss01`, `cv11` |
| Numbers, labels, locators | Geist Mono 400 / 500 | `.num` for tabular figures. `.section-label`: 11 px, uppercase, 0.12em tracking |
| Page titles | Geist 600 | 28 px, −0.03em |
| Landing display | Geist 600 | Hero 38 to 60 px (−0.045em), section heads 28 to 40 px |

Both fonts load from Google Fonts with `display=swap`, and fall back to the system UI and monospace stacks.

## 4. Shape, space and motion

| Token | Value | Use |
|---|---|---|
| `--r-sm` | 4 px | Tags, small controls, focus outline |
| `--r` | 6 px | Buttons, inputs, columns |
| `--r-lg` | 10 px | Cards, tables, stat grids |
| `--shadow` | inset hairline + soft drop | Drawer and overlays only |
| `--ease` | `cubic-bezier(0.2, 0.7, 0.2, 1)` | Drawer, nav, transitions |

Motion is short (120 to 200 ms) and functional: the drawer slides in, a cited card flashes amber once when its citation is clicked, skeletons shimmer. `prefers-reduced-motion: reduce` turns animation and transitions off.

## 5. Layout

- **Workspace:** a fixed sidebar (brand, navigation, provider status, counts) and a scrolling view. Under 900 px the sidebar becomes an off-canvas menu behind a top bar.
- **Page header:** title, one line of description, actions on the right.
- **Evidence over cards.** Results are rows: thumbnail, number, source, locator, the matched text, why it was returned. Lists use hairline dividers rather than card grids.
- **Evidence drawer:** opens from the right over a scrim. It shows the player or page at the cited place, speech with clickable timestamps, the image with matched regions outlined in amber, entities and links.
- **Side by side:** the baseline comparison and the evaluation's per-question view put the text-only result and Weft in two equal columns, which stack under 720 px.
- **Widths:** layouts work from 360 px. Tables scroll horizontally inside their card rather than the page.

## 6. Components

Primitives are in `weft.css`. Workspace components are in `app.css`, built with the helpers in `static/js/app/ui.js`.

| Component | Notes |
|---|---|
| `.btn` | Default (surface), `.btn-primary` (light fill, one per view), `.btn-ghost`, `.btn-danger` (red on hover). Sizes `.btn-sm` (28 px), default (36 px), `.btn-lg` (44 px). `.btn-icon` for square icon buttons |
| `.input`, `.select` | 38 px, `bg-raised`, amber focus glow |
| `.chip` | Pill filter or label. `aria-pressed="true"` marks the selected one. `.chip.mod` is a modality chip in its thread colour |
| `.tag` | Small mono label: entity types, question types, relation names |
| `.badge` | Bordered status label. `.badge-warn` for warnings such as a proxy evaluation |
| `.meter` | Confidence bar plus percentage. Green from 0.8, amber from 0.6, red below |
| `.card`, `.card-pad` | Surface with a hairline border and 10 px radius |
| `.table` | Mono uppercase headers, 12 to 16 px cells, hover rows, `.row-link` for clickable rows |
| `.stats` / `.stat` | Grid of figures separated by hairlines |
| `.ev-card` | One piece of evidence: thumbnail, number, source, locator, text, why it matched. `.linked` (dashed border) when it came through a link |
| `.thumb` | Keyframe or page render, or the modality glyph when there is none |
| `.switch` | Toggle for retrieval options |
| `.skeleton` | Shimmer block in the shape of the content |
| `.empty` | Centred title, one line and one action |
| `.toast` | Bottom right, coloured dot for success or error |
| `kbd` | Keyboard hints (`/` and `Ctrl/⌘ K` focus Ask) |

All icons are inline SVG on a 16 px grid with 1.5 px strokes, drawn with `currentColor`.

## 7. Data display

- **Locators** are mono: `00:12 - 00:25`, `Page 3`, `ticket-4471`. A locator narrowed to the exact sentence is amber.
- **Percentages** are whole numbers. Rank metrics (MRR, nDCG) use two decimals.
- **Evaluation tables:** one neutral bar per cell, the Weft row tinted with `signal-soft` and its bars amber, the best value per column in bold. Found evidence is a green check, missed evidence is faint.
- **Confidence** is shown as a meter, never as colour alone.

## 8. States and feedback

| State | Treatment |
|---|---|
| Loading | Skeleton rows the shape of the content. Buttons change their label ("Running…") and disable |
| Empty | One line of what's missing and one action, e.g. "Your library is empty" → **Load sample data** |
| Error | Toast with the server's message. Failed views show an empty state with the error |
| Server down | The sidebar status reads "Server unreachable" |
| Providers off | The sidebar shows which of vision, transcription, speakers and entities are on |
| Background work | Upload rows show stage and progress until the job finishes |

## 9. Accessibility

- Every control is reachable by keyboard. `:focus-visible` draws a 2 px amber outline.
- Server content is inserted as text nodes only, never parsed as HTML.
- Navigation marks the current page with `aria-current="page"`. Toggles use `aria-pressed`. Icon-only buttons have `aria-label`.
- Modality and status are always also given as text or a glyph, not by colour alone.
- `--faint` is reserved for de-emphasised content and is below body-text contrast. It is not used for anything a user must read to complete a task.
- Reduced motion is respected.

## 10. Writing

- Plain and specific: "No evaluation has been run yet", not "Error 404".
- Say where things are: "incident-review.mp4 · 00:12 - 00:25", "Page 3".
- Explain results in one short clause: "Reached through a cross-modal link", "matched in the slide text".
- Errors say what happened and what to do next. Server messages are written to be shown as they are.
