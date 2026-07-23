# Step 2 — Redesign the product experience

## Goal

Turn the current functional localhost UI into a calm, distinctly human desktop
experience for exploring a private family archive.  This is a visual and
interaction redesign of the existing HTML/CSS/JavaScript application; it does
not introduce Electron/Tauri or alter catalogue semantics.

The product should feel like *a personal archive and memory room*, not an AI
dashboard, a cloud photo service, or an admin panel.  The media should carry the
visual weight.  Interface chrome should be quiet, legible, and useful.

## Confirmed Step 1 baseline

This plan is based on the implemented paths/configuration work:

- Fresh `Config.load()` has `roots=[]`, with app state under the per-user data
  directory.
- `oa migrate-data` safely copies legacy project-local `data/` into that location.
- The GUI already has an archive-picker concept and APIs for listing and adding
  archive roots.
- `cmd_gui` still refuses to launch when no database exists.  That must change in
  this step: first-run onboarding cannot appear if the GUI cannot start.

## Decision gate — choose a visual direction first

Choose one primary direction below.  Do not blend all four: that is how a product
ends up looking generic.

### A. Warm editorial archive — recommended

**Feeling:** a beautifully cared-for family photo book; quiet, tactile, enduring.

- Warm paper background (`#F5F1E8`), ink/navy text, muted terracotta accent.
- Serif display type for section titles; clean sans-serif for controls and metadata.
- Large date/location headings, generous whitespace, uneven-but-intentional
  editorial image rhythm in overview views.
- Dark mode is optional and should be a true charcoal/cream counterpart, not
  blue-grey developer chrome.

Use the restraint and photo-first browsing model of Apple Photos as a reference,
but give this app its own warmer archival personality.

### B. Modern darkroom

**Feeling:** a serious local media workstation; cinematic and focused.

- Charcoal/near-black canvas (`#151412`), soft ivory text, one muted gold or sage
  accent.
- Edge-to-edge image grid; controls recede until hovered or needed.
- Compact left rail and a filmstrip-like detail view.
- Best if the expected use is long desktop sessions reviewing a huge collection.

Take the useful library/navigation patterns of Immich, but remove cloud-server
language, dense administration, and bright blue emphasis.

### C. Light, familiar library

**Feeling:** immediately approachable—like the best parts of a familiar photo app.

- Clean off-white background, slim navigation, rounded but restrained controls.
- Chronological grid is the home view; people, map, duplicates, and folders are
  supporting routes.
- Fast search and filters are prominent; setup language is plain and reassuring.
- Lowest learning curve, but needs careful typography and spacing to avoid looking
  like a generic Material dashboard.

### D. Archive desk / research cabinet

**Feeling:** a tool for preserving, understanding, and gradually repairing a
collection spanning decades.

- Neutral stone/ink palette, structured grids, small metadata labels and provenance
  visible when wanted.
- Timeline, folders, duplicates, and metadata confidence are first-class.
- Appropriate for documents/audio as well as photographs.
- More distinctive and intellectually satisfying, but less immediately emotional
  than direction A.

### References (for inspiration, not imitation)

- Apple Photos demonstrates a photo-led library with timeline and people views:
  <https://support.apple.com/guide/photos/get-started-pht23b129fed/mac>
- Immich is useful reference material for its date-grouped grid, sidebar, map, and
  people routes: <https://immich.app/>
- Mylio is the closest reference for a local-first lifetime archive organized by
  time, people, places, folders, and duplicates: <https://mylio.com/details/>

## Selected direction — COA Noir, adapted for the archive

Adopt the established **NOIR** visual language from
`/media/capsa/Programas/coa-web/1/coa-web` as the direct design reference for this
project.  This is the user's own visual system, so reuse it faithfully rather than
creating a second unrelated dark theme.  Adapt its layout to photo browsing; do
not copy COA's knowledge-graph, vault, or dashboard interaction patterns.

### Intended character

- **Cinematic, not futuristic:** near-black surfaces, parchment text, and the COA
  personal-vault terracotta accent—not blue-grey SaaS chrome.
- **Warm, not sepia:** the interface is warm while media is always rendered
  untouched.
- **Confidently quiet:** photographs, titles, and date groupings lead; outlines,
  dividers, badges, and count cards recede.
- **Archival, not sterile:** metadata is precise when opened but does not compete
  with browsing.

### Foundation tokens

Port these initial tokens from COA's existing `app/styles/variables.css`; preserve
their names where practical so the two applications visibly belong to the same
family.  Tune only after rendering actual archive media at normal desktop scale.

```css
:root {
  --asphalt: #0E0E0E;           /* page / image-viewer surround */
  --charcoal: #141414;          /* rail */
  --surface: #1C1C1C;           /* raised panels */
  --surface2: #252525;          /* hover / field */
  --parchment: #F0EAD6;
  --parchment-muted: #A89E8A;
  --parchment-faint: #3A352C;
  --accent: #B5614A;            /* COA personal-vault: selected / primary action */
  --accent-dim: #6B3828;
  --accent-rgb: 181, 97, 74;
  --tungsten: #E8A842;          /* active work / warning */
  --tungsten-dim: #6B4E1F;
  --crimson: #8B2E2E;
  --crimson-bright: #C45A5A;
}
```

Use `--accent` / `--accent-dim` for the active navigation edge and fill, primary
buttons, focus rings, selection states, archive identity, and subtle separator
tints.  Do **not** carry over COA's community (teal) or organizational (slate)
type colors; this application has one archive identity, not vault categories.

- Display/navigation: `Bebas Neue`, `Impact`, sans-serif, for the application
  identity and compact uppercase route labels.
- Titles/date headings: `Playfair Display`, `Georgia`, serif.
- Long explanatory copy: `Newsreader`, `Georgia`, serif.
- Controls, paths, file facts, counts: `JetBrains Mono`, `Courier New`, monospace.
- Port the Google Fonts import from COA, or self-host those exact font files later
  during the packaging step.  Do not substitute generic system UI type.
- Radius: 0px for small controls and tags, 4px for side-nav items, 8px only for
  dialogs and large panels. Image tiles are square or nearly square, like prints.
- Shadows: almost none; use COA's thin brass-tinted separators and tonal surfaces.

### Layout signature

1. A COA-style **56px top navigation band** carries the `ARCHIVE` identity, archive
   name, small quiet status, and settings.  Use subtle terracotta top/bottom rules.
2. A **220px left navigation panel**, collapsible to a 52px icon rail, carries the
   archive switcher and primary routes. Reuse COA's personal-vault active-edge
   treatment in terracotta.
3. The Library is an **edge-conscious chronological wall**: tight image gaps,
   large date headings, and a floating year index at the far right while scrolling.
4. The selected image opens against a near-black field with a right-side info
   drawer.  The drawer resembles a well-set caption sheet, rather than a settings
   form.
5. Scan activity is a small warm status line—never a dashboard widget or persistent
   progress card unless a job needs attention.

### Non-negotiable anti-patterns

- No electric blue primary buttons, gradients except COA's extremely subtle active
  navigation fill, glow effects, glassmorphism, oversized rounded cards, sparkles,
  or “AI” badges.
- No icon-only action without an accessible label/tooltip.
- COA's film-grain overlay may be used at its current very low opacity on the app
  shell, but it must never overlay the image lightbox or alter thumbnails/media.
- No faux-polaroid frames, paper textures, or sepia filters on user media.
- No dense dashboard of metrics above the library.

The remainder of this plan uses Night Archive as its visual direction.

## Experience principles

1. **Photos first.** Do not lead with charts, counters, or pipeline controls.
2. **Privacy is a promise, not decoration.** State “stored and processed on this
   computer” at setup and settings moments; do not repeat it as a badge everywhere.
3. **Nothing is destructive.** Every potentially worrying action says what changes
   and what does not.  Duplicates are grouped, never deleted.
4. **Progress belongs in context.** A scan should be visible and understandable
   without taking over every route.
5. **Progressive disclosure.** A casual browser sees memories; a careful archivist
   can reach paths, sources, dates, and confidence information in the detail panel.
6. **No decorative AI language or sparkles.** Face and semantic features are clear
   local tools, not magical assistants.

## Information architecture

Use one stable application shell after onboarding:

```text
Archive name / switcher
├── Library                  chronological home / search / filters
├── Timeline                 years, months, events
├── People                   face groups and review
├── Places                   map and named places
├── Duplicates               review groups; never delete originals
├── Folders                  source-tree browsing
└── Archive settings         sources, scan status, metadata, removal
```

Keep feature routes that are unavailable, but present them as useful empty states
within their own views—not disabled sidebar entries labelled “soon.”

## Required user flows

### 1. First run: begin an archive

1. GUI starts successfully with no DB and no roots.
2. Welcome screen says what the app does in one sentence: it catalogues files in
   place; it never moves, edits, uploads, or deletes originals.
3. Primary action: **Choose a media folder**.  In the web UI for now this can be a
   validated path input; native folder selection belongs to the desktop-shell step.
4. Optional secondary action: add more folders.
5. Confirmation presents selected folders and a plain-language “Start cataloguing”
   action.
6. The app creates/initializes its DB, registers roots, then opens the archive with
   visible scan progress.

Do not expose a blank archive picker, a raw API error, or CLI instructions to a
first-time GUI user.

### 2. Returning home: library, not dashboard

The home route is **Library**.  It contains:

- a large, useful search field;
- a chronological media grid with human date headings;
- an unobtrusive year jump control;
- compact filters for media type, date, folder, and location;
- a scan-status line that expands only when clicked;
- an empty/partially-indexed state that explains what is still becoming available.

Move numerical archive summaries and task queues to Archive settings or a small
secondary overview, rather than making them the first visual impression.

### 3. Explore and inspect

- Clicking an item opens a focused lightbox/detail view.
- The image/video is dominant; next/previous navigation is quick.
- A right information panel groups: date, place, people, original path, source,
  dimensions/duration, and duplicate status.
- Manual corrections remain obvious, reversible, and scoped to catalogue metadata.
- On medium screens, the information panel becomes a drawer; keyboard navigation
  (`←`, `→`, `Esc`) works.

### 4. Archive maintenance

- Surface scan, enrich, dedup, and faces work as a single “Archive activity” area
  with clear state: not started, working, paused/error, or current.
- Never call this a “pipeline” in user-facing copy.
- Duplicates show a clear canonical/redundant relationship and repeat “No files are
  deleted.”
- Archive removal requires deliberate confirmation and names only the catalogue
  record/cache effects, never the source media.

## Implementation plan

### 1. Make first-run GUI-capable

- Remove the early `cmd_gui` failure when `archive.db` is absent.
- Have `serve()` initialize its SQLite schema/DB before the request handler and job
  manager depend on it.
- Make `/api/archives` return an empty list safely for a first run.
- Reuse the current archive-add API to save roots and initialize a new archive;
  add the smallest API change necessary to start its initial scan.
- Add backend tests for launching/serving with a fresh, isolated `XDG_DATA_HOME`.

### 2. Establish a design system before rewriting markup

Replace the current one-line blue-grey CSS tokens with named semantic tokens:

- surfaces, text, quiet text, border, focus, primary action, warning, success;
- spacing scale; radii; shadows; responsive breakpoints;
- display and UI font stacks; tabular-number rule for counts;
- interaction states (hover, focus-visible, selected, disabled).

Use CSS custom properties.  No component framework is required for this step;
keeping the existing dependency-free single-page UI is a sound scope boundary.

### 3. Rebuild shell and navigation

- Replace the bulky dashboard-style sidebar with a quiet rail that has clear route
  labels and an archive switcher.
- Add a compact global search affordance and an activity indicator.
- Preserve mobile/narrow-screen usability: the rail becomes an overlay or top bar.
- Remove visual “soon” tags; use route-specific empty states instead.

### 4. Rebuild the five key screens

Implement and manually review at desktop widths 1280px and 1536px, plus 768px:

1. welcome + archive source selection;
2. Library; 
3. item lightbox/detail;
4. People and Places;
5. Duplicates and Archive settings/activity.

Use representative local images only.  Do not add fabricated stock-family photos
to the shipped product.

### 5. Accessibility and polish

- Semantic buttons/labels, visible keyboard focus, and non-color-only status.
- Sufficient contrast in both themes if both are implemented.
- `prefers-reduced-motion` support; no gratuitous animated counters or skeletons.
- Preserve text handling for long folder paths, unknown dates, missing thumbnails,
  audio, documents, and videos.
- Keep image grids performant: lazy-load thumbnails and retain the existing paging
  model.

### 6. Validate

- Run `pytest -q` including the new first-run GUI tests.
- Test fresh app data, an archive with no scanned files, a partially scanning
  archive, and a populated archive.
- Manually test keyboard detail navigation and an archive removal confirmation.
- Check that no original media file is ever written, moved, or deleted.

## Definition of done

- A person with no configured archive can open the GUI and begin safely.
- The primary view feels photo-led and deliberate, with a chosen visual identity.
- Every existing primary capability remains reachable: browse, dates/timeline,
  places, people, duplicates, folders, item metadata, and archive management.
- The UI is usable at the specified responsive widths and by keyboard.
- No Electron/Tauri files, installer logic, or native-folder APIs are introduced.

## Explicitly out of scope

- Desktop wrapper, installer, signing, updates, and bundled binaries.
- Changing the scanning/indexing/face/dedup algorithms.
- Cloud accounts, sharing, or remote sync.
- Replacing the current frontend with React/Vue/etc.

## Handoff to Step 3

After this redesign is complete, document the final app-launch contract (how a
fresh GUI initializes, its port/health endpoint, and graceful shutdown).  Step 3
will use that stable contract to add the Electron desktop shell and platform
packaging.
