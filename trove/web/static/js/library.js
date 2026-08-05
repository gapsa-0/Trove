// The Library screen: the media grid, its bidirectional paging, the filter bar
// whose controls all compose with one another, and the tile that every grid in
// the app renders. Search is next door in search.js; this module owns what the
// grid does with whatever query it is given.

import {
  ACTIVE_SECTION, libraryVisibleAnchor, restoreLibraryAnchor,
} from "./router.js";
import {
  jget, jpost,
} from "./api.js";
import {
  docsButton,
} from "./docs.js";
import {
  esc, fmtDate, toast,
} from "./dom.js";
import {
  openItem,
} from "./item.js";
import {
  onSemanticComposerInput, renderActiveQuery, renderSearchReach, renderSemanticComposer,
  setPeopleChecks, warmLocalTranslator,
} from "./search.js";
import {
  S, TYPE_ICON, archiveHasFeature, typeLabel,
} from "./state.js";
import {
  applyTimelineFilters,
} from "./timeline.js";

export const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
const GRID_PAGE_SIZE = 120, GRID_MAX_PAGES = 4;
// The filter bar is the one part of this screen that cannot be drawn without
// the server: which years, which people, which places *this* archive has. That
// answer is a pass over every file in it, so on a cold page cache -- exactly
// the state just after an archive is opened, when the pipeline is competing
// for the same disk -- it takes seconds. Standing in for the controls with
// pills of their own size holds the toolbar in the shape it will settle into,
// instead of leaving an empty row with the sort select adrift at its far end.
// Replaced wholesale by buildFilterBar, so it needs no clearing of its own.
const FILTER_SKELETON = [108, 120, 104, 98, 116]
  .map(w => `<span class="fsel fsel-loading" style="width:${w}px" aria-hidden="true"></span>`)
  .join("");
/* Which elements a grid owns, and what it asks for.

   Browse shows up to two result groups, and they page identically -- same
   bidirectional window, same sentinel margins, same generation guard -- so they
   run through one implementation rather than two that drift. What differs is
   only which elements a grid draws into and which endpoint answers it, so that
   is what lives on the grid object. */
const GRID_IDS = {
  media: { grid: "grid", top: "grid-top-sentinel", bottom: "grid-sentinel", count: "gridcount" },
  text: { grid: "grid-text", top: "grid-text-top", bottom: "grid-text-sentinel", count: "gridcount-text" },
};
// The filter and query state both groups search under. Held once, on the media
// grid, and copied across before a reload rather than edited in two places --
// two grids narrowing by different years is a bug with no symptom until someone
// notices the totals disagree.
const SHARED_QUERY_FIELDS = ["year", "month", "type", "people", "inferredPeople", "place",
  "rawQuery", "searchedQuery", "query", "expandedQuery", "sort", "topMatchesOnly"];
function newGrid(kind) {
  return {
    kind, ids: GRID_IDS[kind],
    offset: 0, loaded: 0, gen: 0, year: "", month: "", type: "", people: [], inferredPeople: [],
    place: "", rawQuery: "", searchedQuery: "", query: "",
    expandedQuery: "", sort: "", error: "", topMatchesOnly: true,
    total: null, doneDown: false, doneUp: true, loadingGen: null, observer: null, pages: [],
    anchor: null, savedScrollTop: 0,
  };
}
// Every grid currently on screen. The text one only exists for an archive that
// reads its documents, and only says anything while a search is active.
export function activeGrids() {
  return S.textGrid ? [S.textGrid, S.grid] : [S.grid];
}
function shareQueryState() {
  if (!S.textGrid) return;
  SHARED_QUERY_FIELDS.forEach(f => {
    S.textGrid[f] = Array.isArray(S.grid[f]) ? S.grid[f].slice() : S.grid[f];
  });
}
// Reset and reload every group at once. Filters, sort and the search itself all
// apply to both, so each of them goes through here rather than reloading the
// grid it happens to be holding.
export function reloadGrids() {
  shareQueryState();
  activeGrids().forEach(g => { resetGridResults(g); loadGrid("append", g); });
}
export async function renderPhotos(m) {
  const gen = S.nav;
  const restored = !!(S.grid && Array.isArray(S.grid.pages));
  const g = restored ? S.grid : newGrid("media");
  S.grid = g;
  // Documents unlocks no nav section either, so like Search by description it
  // is gated where it is used. An archive that declined it must not be shown a
  // group searching an index whose stage the scheduler will never start.
  const readsText = archiveHasFeature(S.arch, "documents");
  S.textGrid = readsText ? (restored && S.textGrid ? S.textGrid : newGrid("text")) : null;
  refreshGallery();
  /* Neither search feature unlocks a nav section -- both are this one box --
     so what each of them adds is decided here rather than by dropping a
     section, and the box says what it can actually do.

     The box itself is unconditional. Its floor is matching what you type
     against file names, which needs no index, no model and no feature: an
     archive that runs neither search feature can still be asked for
     "escritura" and find escritura-2019.pdf. What the features add on top is
     what the words are matched *against* -- the description index, the
     documents' own text, or both. */
  const searchable = archiveHasFeature(S.arch, "semantic");
  const placeholder = searchable
    ? (readsText
      ? "Search your library — describe a photo, or quote a document"
      : "Search your library, describe anything")
    : (readsText
      ? "Search your documents, or any file by name"
      : "Search your library by file name");
  const blurb = searchable
    ? (readsText
      ? "Look through every item, by filter, by description, or by what it says."
      : "Look through every item, by filter or by description.")
    : (readsText
      ? "Look through every item, by filter, by name, or by what your documents say."
      : "Look through every item, by filter or by file name.");
  m.innerHTML = `<div class="pagehead">
      <div><h2 class="sec">Browse</h2>
      <p>${blurb}</p></div>
      ${docsButton("library")}
    </div>
    <div class="library-controls">
      <form class="library-search" onsubmit="return semanticSubmit(event)">
        <div class="semantic-composer" id="semantic-q" contenteditable="true" role="textbox"
          aria-label="${esc(placeholder)}" data-placeholder="${esc(placeholder)}"
          spellcheck="true" oninput="onSemanticComposerInput()" onkeydown="onSemanticComposerKeydown(event)"
          onpaste="onSemanticComposerPaste(event)"></div>
        <button class="btn" type="submit">Search</button>
      </form>
      <div class="active-query" id="active-query" aria-live="polite" hidden></div>
      ${searchable ? `<div class="search-reach" id="search-reach" aria-live="polite" hidden></div>` : ""}
      <div class="library-toolbar">
        <div class="filterbar" id="filterbar" aria-busy="true">${FILTER_SKELETON}</div>
        <div class="chips">
          <select class="fsel sort-sel" id="f-sort" aria-label="Sort media" onchange="applySort()"></select>
          <span class="muted" id="gridcount"></span>
        </div>
      </div>
    </div>
    ${readsText ? `<section class="results-group" id="group-text" hidden>
      <h3 class="results-label">Matched in text<span class="muted" id="gridcount-text"></span></h3>
      <div class="infinite-status top" id="grid-text-top" aria-hidden="true"></div>
      <div class="grid" id="grid-text"></div>
      <div class="infinite-status" id="grid-text-sentinel" aria-live="polite"></div>
    </section>` : ""}
    <section class="results-group" id="group-media">
      <h3 class="results-label" id="label-media" hidden>Matched by description</h3>
      <div class="infinite-status top" id="grid-top-sentinel" aria-hidden="true"></div>
      <div class="grid" id="grid"></div>
      <div class="infinite-status" id="grid-sentinel" aria-live="polite"></div>
    </section>`;
  const composer = document.getElementById("semantic-q");
  composer?.addEventListener("compositionstart", () => S.composerComposing = true);
  composer?.addEventListener("compositionend", () => { S.composerComposing = false; onSemanticComposerInput(); });
  // Start loading the translation model on the first keystroke, not when the
  // screen opens: it is 23 MB, and opening Library is no evidence that anyone
  // intends to search. The server-side warm-ups can afford to be eager because
  // they cost the visitor nothing; this one is downloaded to their machine.
  //
  // Typing rather than focus, because submitting a search focuses the composer
  // itself (renderSemanticComposer) -- so focus would fire on every search
  // whether or not anyone had touched the box. The rest of the typing still
  // covers the load.
  //
  // Only where a description search can actually use it: translation exists to
  // help the image model, and neither a document's own words nor a file's name
  // is searched in English.
  if (searchable) composer?.addEventListener("input", warmLocalTranslator, { once: true });
  // Everything that needs nothing from the server goes first, and the grid's
  // own fetch is started here rather than after the filters land. The filter
  // options are needed by the filter bar alone -- the grid asks for the pages
  // the *stored* filters describe -- so awaiting them before loading the grid
  // only added one slow request's time to the other, and left the whole screen
  // blank for the sum. Both requests are now in flight together.
  const filters = buildFilterBar();
  // The reach line reports how much of the archive the *description* index
  // covers, so it belongs to that feature alone -- on an archive without it,
  // the line has nothing to report but its own absence, and the element it
  // would fill is not in the markup above either.
  if (searchable) renderSearchReach();
  renderSortOptions(g);
  renderActiveQuery(g);
  if (restored) {
    // Only the composer: the rest of the controls do not exist yet.
    restoreLibraryComposer(g);
    activeGrids().forEach(grid => {
      renderGridPages(grid);
      const count = document.getElementById(grid.ids.count);
      if (count && grid.total != null) count.textContent = gridCountLabel(grid);
    });
    renderGroupLabels();
  }
  activeGrids().forEach(setupGridInfiniteScroll);
  if (restored && g.pages.length) {
    requestAnimationFrame(() => {
      if (ACTIVE_SECTION !== "library" || S.grid !== g) return;
      if (!restoreLibraryAnchor(g.anchor))
        document.getElementById("main").scrollTop = g.savedScrollTop || 0;
    });
  } else activeGrids().forEach(grid => loadGrid("append", grid));
  await filters;
  if (gen !== S.nav || S.grid !== g) return;
  // The bar has restored its own controls by now (drawFilterBar). This line is
  // the other thing that needed the people options: it draws the names inside
  // the search it is reporting as the person filters they became.
  renderActiveQuery(g);
}
function setupGridInfiniteScroll(g) {
  const top = document.getElementById(g.ids.top);
  const bottom = document.getElementById(g.ids.bottom);
  if (!top || !bottom) return;
  if (g.observer) g.observer.disconnect();
  g.observer = new IntersectionObserver(entries => {
    if (!activeGrids().includes(g) || ACTIVE_SECTION !== "library") return;
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      if (entry.target === top && !g.doneUp) loadGrid("prepend", g);
      else if (entry.target === bottom && !g.doneDown) loadGrid("append", g);
    });
  }, { root: document.getElementById("main"), rootMargin: "600px 0px" });
  g.observer.observe(top);
  g.observer.observe(bottom);
}
function gridSentinelIsNear(direction, g) {
  const sentinel = document.getElementById(direction === "prepend" ? g.ids.top : g.ids.bottom);
  const main = document.getElementById("main");
  if (!sentinel || !main) return false;
  const sr = sentinel.getBoundingClientRect(), mr = main.getBoundingClientRect();
  return direction === "prepend" ? sr.bottom >= mr.top - 600 : sr.top <= mr.bottom + 600;
}
/* What the filter bar can offer is a property of the archive -- which years it
   covers, who has been named in it, where its media was taken -- so it is
   fetched once per archive and kept, rather than re-derived on every visit to
   Browse. Deriving it is a pass over every file, seconds of it when the page
   cache is cold, and this screen is left and returned to constantly.

   The cached bar is drawn first and the request still goes out behind it, so a
   year the pipeline has since dated, or a person just named, still appears --
   the bar is only redrawn if the answer actually changed, since redrawing it
   for an identical answer would close an open menu for nothing. */
async function buildFilterBar() {
  const gen = S.nav, rid = S.arch.id;
  const cached = S.filterOptsRoot === rid ? S.filterOpts : null;
  if (cached) drawFilterBar(cached);
  const f = await jget("/api/browse/filters?root=" + rid);
  if (gen !== S.nav) return;
  const changed = !cached || JSON.stringify(f) !== JSON.stringify(cached);
  S.filterOpts = f; S.filterOptsRoot = rid;
  if (changed) drawFilterBar(f);
}
function drawFilterBar(f) {
  const bar = document.getElementById("filterbar"); if (!bar) return;
  const years = [...new Set((f.periods || []).map(p => p.slice(0, 4)))];
  const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
  const opt = (v, l) => `<option value="${v}">${l}</option>`;
  const parts = [];
  if (years.length)
    parts.push(`<select class="fsel" id="f-year" onchange="onYearChange()">` +
      opt("", "All years") + years.map(y => opt(y, y)).join("") + `</select>` +
      `<select class="fsel" id="f-month" onchange="applyFilters()" disabled>` +
      opt("", "All months") + `</select>`);
  if (f.types && f.types.length > 1)
    parts.push(`<select class="fsel" id="f-type" onchange="applyFilters()">` +
      opt("", "All types") + f.types.map(t => opt(t, cap(typeLabel(t)))).join("") + `</select>`);
  // People is a checkbox menu because several people can be joined together.
  parts.push(peopleFilterHTML("f", f.people || []));
  parts.push(`<select class="fsel" id="f-place" onchange="applyFilters()" ${f.places && f.places.length ? "" : "disabled"} title="${f.places && f.places.length ? "Filter by place" : "Name places in Places to enable this filter"}">` +
    opt("", f.places && f.places.length ? "All places" : "No places named yet") + (f.places || []).map(p => opt(p.id, esc(p.name))).join("") + `</select>`);
  // The result-scope toggle is deliberately not here: it does not narrow the
  // library the way these do, it says how much of one search's ranking you are
  // looking at. It lives on the search's own line instead (renderActiveQuery).
  parts.push(`<button class="linkbtn" id="f-clear" onclick="clearFilters()" style="display:none">Clear filters</button>`);
  bar.innerHTML = parts.join("");
  bar.removeAttribute("aria-busy");
  // Every drawing of the bar puts the grid's own filters back onto it -- the
  // first one, and any redraw after the options changed under it. The grid is
  // what the screen is actually showing, so it is the authority on what the
  // controls should read; on a fresh grid every value is empty and this is the
  // no-op it looks like.
  if (S.grid) restoreLibraryFilters(S.grid);
  renderSemanticComposer(false);
}
function setLibraryMonthOptions(y, selected = "") {
  const msel = document.getElementById("f-month");
  if (msel) {
    if (!y) { msel.innerHTML = '<option value="">All months</option>'; msel.disabled = true; }
    else {
      // .filter(Boolean): a year-only period ("2024", from a manual year-precision
      // date) yields an empty month slice, so drop it so no blank month option appears.
      const months = [...new Set((S.filterOpts.periods || [])
        .filter(p => p.slice(0, 4) === y).map(p => p.slice(5, 7)).filter(Boolean))].sort();
      msel.innerHTML = '<option value="">All months</option>' +
        months.map(mm => `<option value="${mm}">${MONTH_NAMES[+mm - 1]}</option>`).join("");
      msel.disabled = false;
      msel.value = selected;
    }
  }
}
/* Sort: date only for now. "" means the list's natural order -- best match
   while a description search is active, newest first when just browsing --
   so the option only needs spelling out as "Newest first" when there is no
   search to rank against. */
export function renderSortOptions(g) {
  const sel = document.getElementById("f-sort"); if (!sel) return;
  // A filename search has no ranking of its own, so it offers no "best match"
  // to sort by -- the grid stays in date order and only the set narrows.
  const opts = g.query && mediaRanksQueries()
    ? [["", "Best match"], ["newest", "Newest first"], ["oldest", "Oldest first"]]
    : [["", "Newest first"], ["oldest", "Oldest first"]];
  sel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  // Leaving a search collapses "newest" onto the default, which now means
  // the same thing, so the grid keeps the order the user was looking at.
  if (!opts.some(([v]) => v === g.sort)) g.sort = "";
  sel.value = g.sort;
}
function gridCountLabel(g) {
  const n = (g.total || 0).toLocaleString();
  if (g.kind === "text") return `${n} document${g.total === 1 ? "" : "s"}`;
  return g.query ? `${n} match${g.total === 1 ? "" : "es"}` : `${n} files`;
}
export function applySort() {
  S.grid.sort = selVal("f-sort");
  reloadGrids();
}
/* Coming back to the Library puts the user's controls back the way they left
   them, in two halves, because the two halves become available at different
   times. The composer is in the shell markup, so a half-typed search is back
   on screen immediately; the filter controls do not exist until the archive's
   options have been fetched. Splitting them is what lets the composer be
   restored without waiting for that fetch.

   The composer's text is written once, here, and never rewritten afterwards:
   by the time the filters land the user may have typed into it, and
   `renderSemanticComposer` re-reads whatever the box now says. */
function restoreLibraryComposer(g) {
  const composer = document.getElementById("semantic-q");
  if (composer) { composer.textContent = g.rawQuery || ""; renderSemanticComposer(true); }
}
function restoreLibraryFilters(g) {
  const year = document.getElementById("f-year"); if (year) year.value = g.year || "";
  setLibraryMonthOptions(g.year, (g.month || "").slice(5, 7));
  const type = document.getElementById("f-type"); if (type) type.value = g.type || "";
  const place = document.getElementById("f-place"); if (place) place.value = g.place || "";
  setPeopleChecks("f", g.people || []);
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  updateClearBtn();
}
export function onYearChange() {
  const y = document.getElementById("f-year").value;
  setLibraryMonthOptions(y);
  applyFilters();
}
export function selVal(id) { const e = document.getElementById(id); return e ? e.value : ""; }
export function peopleFilterHTML(prefix, people) {
  if (!people.length)
    return `<span class="fsel filter-placeholder" title="Name people in People to enable this filter">No people named yet</span>`;
  return `<details class="multi-filter" id="${prefix}-people-filter">
    <summary class="fsel"><span id="${prefix}-people-label">Anyone</span></summary>
    <div class="multi-menu">${people.map(p => `<label class="multi-option">
      <input type="checkbox" value="${p.id}" onchange="onPeopleFilterChange('${prefix}')"><span>${esc(p.name)}</span>
    </label>`).join("")}
    <div class="multi-help">Selecting more than one person shows media containing everyone selected.</div></div>
  </details>`;
}
export function checkedPeople(prefix) {
  return [...document.querySelectorAll(`#${prefix}-people-filter input:checked`)].map(e => e.value);
}
export function clearPeopleChecks(prefix) {
  document.querySelectorAll(`#${prefix}-people-filter input:checked`).forEach(e => e.checked = false);
}
export function updatePeopleFilterLabel(prefix, people) {
  const label = document.getElementById(`${prefix}-people-label`); if (!label) return;
  const ids = checkedPeople(prefix), names = ids.map(id => (people.find(p => String(p.id) === id) || {}).name).filter(Boolean);
  label.textContent = !names.length ? "Anyone" : names.length === 1 ? names[0] :
    names.length === 2 ? `${names[0]} + ${names[1]}` : `${names.length} people together`;
  label.closest("summary").title = names.length > 1 ? "Only media containing all selected people" : names.join("");
}
export function onPeopleFilterChange(prefix) {
  if (prefix === "tl") applyTimelineFilters();
  else { if (S.grid) S.grid.inferredPeople = []; applyFilters(); }
}
export function applyFilters() {
  const g = S.grid;
  g.year = selVal("f-year");
  const mm = selVal("f-month");
  g.month = (g.year && mm) ? `${g.year}-${mm}` : "";
  g.type = selVal("f-type");
  g.people = checkedPeople("f");
  g.place = selVal("f-place");
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  updateClearBtn();
  reloadGrids();
}
export function resetGridResults(g) {
  g.offset = 0; g.loaded = 0; g.total = null; g.error = ""; g.doneDown = false; g.doneUp = true; g.gen++;
  g.pages = []; g.anchor = null; g.savedScrollTop = 0;
  refreshGallery();
  const grid = document.getElementById(g.ids.grid); if (grid) grid.replaceChildren();
  const count = document.getElementById(g.ids.count); if (count) count.textContent = "";
  const main = document.getElementById("main"); if (main) main.scrollTop = 0;
}
/* The viewer walks whatever is on screen, in the order it is on screen -- so
   with two groups showing it has to be both of them, text first, matching the
   document order. Rebuilt from the grids rather than appended to, because a
   group can reload independently of the other. */
function refreshGallery() {
  S.gallery = activeGrids().flatMap(g => g.pages.flatMap(p => p.items.map(i => i.id)));
}
/* The group headings only earn their space when there are two groups to tell
   apart. Browsing with no query shows one grid and no labels at all; a search
   with no text hits hides that group rather than leaving an empty heading over
   a blank row.

   The media group is never hidden: whatever the archive runs, it has an answer
   to a query -- the description ranking where that exists, and the files whose
   names match everywhere else -- so its heading says which of the two the row
   below it is. */
function renderGroupLabels() {
  const textGroup = document.getElementById("group-text");
  const mediaLabel = document.getElementById("label-media");
  const searching = !!S.grid.query;
  const textHits = !!(S.textGrid && S.textGrid.total);
  if (textGroup) textGroup.hidden = !(searching && textHits);
  if (mediaLabel) {
    mediaLabel.hidden = !(searching && textHits);
    mediaLabel.textContent = mediaRanksQueries() ? "Matched by description" : "Matched by name";
  }
}
/* Whether a typed query reaches the media grid as a ranking or as a filter.
   With Search by description on it is a ranking, so "best match" is an order
   and the relevance cuts have something to widen; without it the words are
   matched against file names, which is a narrowing of the same dated listing
   and has neither. */
export function mediaRanksQueries() {
  return archiveHasFeature(S.arch, "semantic");
}
export function clearFilters() {
  ["f-year", "f-type", "f-place"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
  clearPeopleChecks("f");
  S.grid.inferredPeople = [];
  const msel = document.getElementById("f-month");
  if (msel) { msel.innerHTML = '<option value="">All months</option>'; msel.disabled = true; }
  applyFilters();
}
export function updateClearBtn() {
  const g = S.grid, b = document.getElementById("f-clear");
  // Deliberately not counting the result-scope toggle: it is not a filter on
  // the library, it is which slice of one search's ranking is on screen, and
  // it clears with the search rather than with these.
  if (b) b.style.display = (g.year || g.month || g.type || g.people.length || g.place)
    ? "inline" : "none";
}
/* Why the empty state is painted from the render and not from the response:
   a grid restored by renderPhotos replays its stored pages without fetching
   again, and a search that matched nothing stores one page holding zero items
   -- so the message has to be reachable without a response in hand, or coming
   back to the Library leaves a blank grid explaining nothing.
   `total` is the "a load has landed" signal: it is null until the first
   response, which is what keeps this off the screen while the first page is
   still in flight. */
function renderGridEmptyState(g, grid) {
  if (g.loaded || g.total == null) return;
  // The text group is hidden outright when it has nothing, so this is only ever
  // the media group's message.
  if (g.kind === "text") return;
  const nothing = g.query && !mediaRanksQueries()
    ? "No file names match this search."
    : "No media matches these filters.";
  grid.innerHTML = `<div class="muted" style="grid-column:1/-1;padding:40px;text-align:center">${
    g.error ? esc(g.error) : nothing}</div>`;
}
function renderGridPages(g, anchor = null) {
  const grid = document.getElementById(g.ids.grid); if (!grid) return;
  grid.replaceChildren();
  let lastHeading = null;
  g.pages.forEach(page => page.items.forEach((item, itemOffset) => {
    // Month headings only make sense while the grid is in date order, which a
    // text group ranked by relevance never is -- and which a name search always
    // is, being a filter on the dated listing rather than a ranking of its own.
    if (g.kind !== "text" && (!g.query || g.sort || !mediaRanksQueries())) {
      const heading = item.date ? (item.date.slice(0, 7) || "Unknown date") : "Unknown date";
      if (lastHeading !== heading) {
        const h = document.createElement("div"); h.className = "date-heading";
        h.textContent = item.date ? fmtDate(item.date.slice(0, 7)) : "Unknown date";
        grid.appendChild(h); lastHeading = heading;
      }
    }
    grid.appendChild(g.kind === "text"
      ? textTile(item) : tile(item, page.offset + itemOffset, "name"));
  }));
  refreshGallery();
  g.loaded = g.pages.reduce((n, page) => n + page.items.length, 0);
  renderGridEmptyState(g, grid);
  if (anchor) restoreLibraryAnchor(anchor);
}
/* Which endpoint answers this grid, and with what.

   Four cases, one shape: a plain browse, a description search, a text search,
   and a name search. Kept together so that a filter added to one is visibly
   absent from the others rather than quietly missing. The text endpoint takes
   no `type` (every hit is a document by construction) and no `top` (there are
   no relevance cuts to widen -- MATCH is the cut); the name search is a filter
   on the plain listing, so it keeps every parameter that listing takes. */
function gridRequest(g, offset) {
  const p = new URLSearchParams({ root: S.arch.id, offset, limit: GRID_PAGE_SIZE });
  if (g.year) p.set("year", g.year);
  if (g.month) p.set("month", g.month);
  g.people.forEach(id => p.append("person", id));
  if (g.place) p.set("place", g.place);
  if (g.sort) p.set("sort", g.sort);
  if (g.kind === "text") {
    p.append("q", g.query);
    return "/api/browse/text/search?" + p;
  }
  if (g.type) p.set("type", g.type);
  // A query only reaches the description index when this archive has one. With
  // Documents on and Search by description off there is still a search box --
  // it just searches the other group -- and sending its words here would ask an
  // endpoint whose stage never runs.
  if (g.query && mediaRanksQueries()) {
    // When local translation succeeds it replaces, rather than supplements,
    // the Spanish semantic query. This avoids admitting weak matches unique to
    // the original-language vector. English and fallback searches use g.query.
    // Sent verbatim: a query that arrived through the translator is embedded
    // as exactly the English someone could have typed themselves, so the two
    // routes to the same words cannot give different results.
    p.append("q", g.expandedQuery || g.query);
    // Only sent when the user widened the search: absence means the tuned
    // relevance cuts apply, so the common URL stays the default one.
    if (g.topMatchesOnly === false) p.set("top", "no");
    return "/api/browse/semantic/search?" + p;
  }
  // No description index to rank against, so the words are matched against the
  // one thing every archive has read: the names of its own files.
  if (g.query) p.set("name", g.query);
  return "/api/media?" + p;
}
export async function loadGrid(direction = "append", g = S.grid) {
  const gen = g.gen, grid = document.getElementById(g.ids.grid);
  const sentinel = document.getElementById(direction === "prepend" ? g.ids.top : g.ids.bottom);
  // A text group with nothing typed has nothing to search: it is a ranking, not
  // a listing, so an empty query means an empty group rather than every file.
  if (g.kind === "text" && !g.query) {
    g.total = 0;
    renderGroupLabels();
    return;
  }
  if (!grid || g.loadingGen === gen ||
    (direction === "append" && g.doneDown) || (direction === "prepend" && g.doneUp)) return;
  const first = g.pages[0], last = g.pages[g.pages.length - 1];
  const requestedOffset = direction === "prepend"
    ? Math.max(0, (first ? first.offset : 0) - GRID_PAGE_SIZE)
    : (last ? last.offset + last.items.length : 0);
  g.loadingGen = gen;
  let failed = false;
  if (sentinel && direction === "append")
    sentinel.innerHTML = '<span class="spin"></span>Loading files…';
  try {
    const res = await jget(gridRequest(g, requestedOffset));
    // Bail if a newer filter change (or a section switch) superseded this fetch
    // while it was in flight, so a slow response can't paint stale tiles.
    if (!activeGrids().includes(g) || g.gen !== gen) return;
    const items = (res && res.items) || [];
    const count = (res && res.count) || 0;
    g.error = (res && res.error) || "";
    g.total = res && res.total != null ? res.total : (g.total == null ? count : g.total);
    const anchor = libraryVisibleAnchor();
    if (direction === "prepend") g.pages.unshift({ offset: requestedOffset, items });
    else g.pages.push({ offset: requestedOffset, items });
    if (g.pages.length > GRID_MAX_PAGES) {
      if (direction === "prepend") g.pages.pop();
      else g.pages.shift();
    }
    g.pages.sort((a, b) => a.offset - b.offset);
    const windowLast = g.pages[g.pages.length - 1];
    g.offset = windowLast ? windowLast.offset + windowLast.items.length : 0;
    g.doneUp = !g.pages.length || g.pages[0].offset === 0;
    g.doneDown = !!g.error || !windowLast ||
      windowLast.offset + windowLast.items.length >= g.total;
    renderGridPages(g, anchor);
    renderGroupLabels();
    const gc = document.getElementById(g.ids.count);
    if (gc) gc.textContent = gridCountLabel(g);
    const bottom = document.getElementById(g.ids.bottom);
    if (bottom) bottom.textContent = g.doneDown ? "" : "Scroll to load more";
  } catch {
    failed = true;
    if (activeGrids().includes(g) && g.gen === gen && sentinel)
      sentinel.textContent = "Couldn’t load more files. Scroll away and back to retry.";
  } finally {
    if (g.loadingGen === gen) {
      g.loadingGen = null;
      // An initial page may not be tall enough to move the sentinel outside
      // the observer margin. Keep filling until the user has something to
      // scroll, without requiring an intersection state change.
      requestAnimationFrame(() => {
        const done = direction === "prepend" ? g.doneUp : g.doneDown;
        if (!failed && activeGrids().includes(g) && g.gen === gen && !done &&
          ACTIVE_SECTION === "library" && gridSentinelIsNear(direction, g))
          loadGrid(direction, g);
      });
    }
  }
}
/* One thumbnail. `caption` says what the strip along its bottom reads: the
   file's own name in Browse, where the grid is already broken into dated
   sections and repeating the date under every tile says nothing the heading
   above it did not -- and the date on the grids that have no such headings
   (a person's photos, a pet's, a place's), where it is the only thing placing
   the shot in time. */
export function tile(it, resultIndex = null, caption = "date") {
  const d = document.createElement("button"); d.type = "button"; d.className = "tile";
  d.dataset.name = (it.name || "").toLowerCase(); d.dataset.fileId = it.id;
  if (resultIndex != null) d.dataset.resultIndex = resultIndex;
  // The pip is decorative markup, so its meaning rides on the tile's own
  // label instead of adding a second stop per tile for screen readers.
  d.setAttribute("aria-label", (it.name || "Open media item") +
    (it.indexed ? ", indexed for description search" : "") +
    (it.has_gps ? ", has a location" : ""));
  d.onclick = () => openItem(it.id);
  if (it.type === "image" || it.type === "video") {
    const img = document.createElement("img"); img.loading = "lazy";
    img.src = "/thumb/" + it.id; img.onerror = () => img.replaceWith(ph(TYPE_ICON[it.type] || "🖼️")); d.appendChild(img);
  }
  else d.appendChild(ph(TYPE_ICON[it.type] || "📦"));
  const cap = document.createElement("div"); cap.className = "cap";
  // A name is arbitrary user data and long enough to need cutting off, so it is
  // escaped, truncated by CSS, and given a title carrying the whole of it.
  const name = it.name || "";
  const label = caption === "name"
    ? `<span class="cap-label" title="${esc(name)}">${esc(name)}</span>`
    : `<span class="cap-label">${(it.date || "").slice(0, 10)}</span>`;
  // `indexed` is absent on description-search results -- every hit there is
  // indexed by definition, so the pip would mark all of them and say
  // nothing. Undefined simply renders no pip, which is the wanted result.
  cap.innerHTML = label + `<span class="cap-marks">` +
    (it.indexed ? `<span class="indexed" title="Indexed for description search"></span>` : "") +
    (it.type === "video" ? "<span>▶</span>" : "") + `</span>`;
  d.appendChild(cap);
  // aria-hidden because the meaning is already on the tile's own label:
  // left alone a screen reader announces the raw emoji ("pushpin"), which
  // is the glyph rather than what it tells you.
  if (it.has_gps) {
    const b = document.createElement("div");
    b.className = "badge"; b.textContent = "📍";
    b.title = "Has a location"; b.setAttribute("aria-hidden", "true");
    d.appendChild(b);
  }
  return d;
}
function ph(icon) { const s = document.createElement("div"); s.className = "ph"; s.textContent = icon; return s; }
/* A library tile plus the passage that matched -- for the text results group
   ONLY. tile() itself is shared with four other grids and must never grow this,
   so the snippet is attached afterwards, the way personTile attaches its own
   control.

   The snippet arrives with the match wrapped in two control characters rather
   than in markup. FTS5 does not escape the document text around the match, so
   returning `<mark>` from the server would mean a document containing the word
   "<script>" could put it into the page. Escaping first and substituting after
   is what keeps the highlight from being an injection point. */
export function textTile(it) {
  const d = tile(it, null, "name");
  if (!it.snippet) return d;
  const box = document.createElement("div");
  box.className = "tile-snippet";
  box.innerHTML = esc(it.snippet)
    .replaceAll("\u0002", "<mark>").replaceAll("\u0003", "</mark>");
  if (it.page != null) {
    const page = document.createElement("span");
    page.className = "tile-page";
    page.textContent = it.page_last && it.page_last !== it.page
      ? `pp. ${it.page}–${it.page_last}` : `p. ${it.page}`;
    box.prepend(page);
  }
  d.appendChild(box);
  return d;
}
// A library tile plus a "not this person" control, for the person detail
// page ONLY -- tile() itself is shared with the plain library grid, which
// must never grow this button. Detach removes the tile optimistically
// (mirrors reassignFace's discipline: mutate/repaint first, roll back +
// toast only if the POST actually fails).
export function personTile(it, personId) {
  const d = tile(it);
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "tile-detach";
  btn.title = "Not this person"; btn.setAttribute("aria-label", "Not this person");
  btn.textContent = "✕";
  btn.onclick = e => { e.stopPropagation(); detachFromPerson(personId, it.id, d); };
  d.appendChild(btn);
  return d;
}
function detachFromPerson(personId, fileId, tileEl) {
  if (!confirm("Remove this photo from this person? It won’t be suggested for them again.")) return;
  const parent = tileEl.parentNode, next = tileEl.nextSibling;
  tileEl.remove();   // optimistic
  jpost("/api/faces/detach", { person_id: personId, file_id: fileId })
    .then(r => {
      if (!(r && r.ok)) {
        if (parent) parent.insertBefore(tileEl, next);   // roll back
        toast((r && r.error) ? "Couldn’t detach: " + r.error : "Couldn’t detach that photo.", true);
      } else {
        toast("Removed from this person.");
      }
    })
    .catch(() => {
      if (parent) parent.insertBefore(tileEl, next);
      toast("Couldn’t detach: connection error", true);
    });
}
