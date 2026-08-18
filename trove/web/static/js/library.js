// The Library screen: the media grid, its bidirectional paging, the filter bar
// whose controls all compose with one another, and the tile that every grid in
// the app renders. Search is next door in search.js; this module owns what the
// grid does with whatever query it is given.
//
// What a query comes *back* as -- the group per ranking, its heading and count,
// the two rows of it the overview shows and the control that opens one in full
// -- is in results.js, the same seam the stylesheets are split at.

import {
  ACTIVE_SECTION, libraryVisibleAnchor, restoreLibraryAnchor,
} from "./router.js";
import {
  jget,
} from "./api.js";
import {
  docsButton,
} from "./docs.js";
import {
  esc, fmtDate,
} from "./dom.js";
import {
  gridCountLabel, gridPagesFreely, previewCount, previewing, refreshGallery,
  renderGroupLabels, renderGroupMore, resultsGroup,
} from "./results.js";
import {
  onSemanticComposerInput, renderActiveQuery, renderSearchWays, renderSemanticComposer,
  setPeopleChecks, warmLocalTranslator,
} from "./search.js";
import {
  S, archiveHasFeature, typeLabel,
} from "./state.js";
import {
  applyTimelineFilters,
} from "./timeline.js";
// Re-exported rather than moved out of reach: nine modules import these from
// here, and the split is about where the code lives, not about churning every
// call site. library.js renders with them too, hence the import as well.
import {
  nameTile, nameTokens, personTile, petTile, textTile, tile,
} from "./tiles.js";
export { nameTile, personTile, petTile, textTile, tile };

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

   Browse shows up to three result groups, and they page identically -- same
   bidirectional window, same sentinel margins, same generation guard -- so they
   run through one implementation rather than three that drift. What differs is
   only which elements a grid draws into and which endpoint answers it, so that
   is what lives on the grid object.

   Three, not four, and the difference is worth stating because the screen used
   to imply otherwise. There are four *readers* filling indexes -- the name,
   the picture, a document's text layer, writing read off pixels -- but only
   three *rankings* that can answer a query. The last two write into the same
   passages and the same FTS5 index, so which of them found a hit is a property
   of the file rather than of a separate search; splitting them into groups here
   would draw two grids over one ranking. So the readers are what a result is
   labelled with, and the rankings are what gets a group. */
const GRID_IDS = {
  name: { grid: "grid-name", top: "grid-name-top", bottom: "grid-name-sentinel", count: "gridcount-name", more: "more-name" },
  media: { grid: "grid", top: "grid-top-sentinel", bottom: "grid-sentinel", count: "gridcount-media", more: "more-media" },
  text: { grid: "grid-text", top: "grid-text-top", bottom: "grid-text-sentinel", count: "gridcount-text", more: "more-text" },
};
// The filter and query state every group searches under. Held once, on the media
// grid, and copied across before a reload rather than edited in three places --
// two grids narrowing by different years is a bug with no symptom until someone
// notices the totals disagree.
const SHARED_QUERY_FIELDS = ["year", "month", "type", "people", "pets", "inferredPeople", "place",
  "rawQuery", "searchedQuery", "query", "expandedQuery", "sort", "topMatchesOnly"];
function newGrid(kind) {
  return {
    kind, ids: GRID_IDS[kind],
    offset: 0, loaded: 0, gen: 0, year: "", month: "", type: "", people: [], pets: [], inferredPeople: [],
    place: "", rawQuery: "", searchedQuery: "", query: "",
    expandedQuery: "", sort: "", error: "", topMatchesOnly: true,
    total: null, doneDown: false, doneUp: true, loadingGen: null, observer: null, pages: [],
    anchor: null, savedScrollTop: 0,
    // How many results the last preview of this group drew. Kept so a width
    // change can be told from every other reason the screen repaints.
    previewCap: 0,
  };
}
/* Every grid currently on screen, in the order they are drawn.

   Literal matches first, which is the rule the text group already followed for
   being above the photos: an exact word match is explainable in a way a cosine
   is not, and a file's own name is the most literal thing Browse can show you.
   The media grid is last because it is also the plain dated listing, which is
   what fills the screen when nothing has been typed. */
export function activeGrids() {
  return [S.nameGrid, S.textGrid, S.grid].filter(Boolean);
}
function shareQueryState() {
  activeGrids().forEach(g => {
    if (g === S.grid) return;
    SHARED_QUERY_FIELDS.forEach(f => {
      g[f] = Array.isArray(S.grid[f]) ? S.grid[f].slice() : S.grid[f];
    });
  });
}
// Reset and reload every group at once. Filters, sort and the search itself all
// apply to both, so each of them goes through here rather than reloading the
// grid it happens to be holding.
export function reloadGrids() {
  shareQueryState();
  // The observers are re-attached rather than left alone: which group may page
  // depends on the query and on which one is open, and both can have moved
  // since they were last hung on these sentinels.
  //
  // Hands back the loads it starts, for the one caller that has to wait on
  // them: a reload that must not move the reader can only put them back once
  // there is something under them again (setResultScope).
  return Promise.all(activeGrids().map(g => {
    resetGridResults(g); setupGridInfiniteScroll(g); return loadGrid("append", g);
  }));
}
/* The ways this archive can answer a typed query.

   Every string in one -- its heading, its mark, the line under it, and which
   pages document it -- is composed by the server from the feature catalogue and
   the pages' own frontmatter (`features.search_ways`, `routes/archives.py`).
   None of it is written here, and that is the point: Browse is the fourth
   screen to name this work, after the setup panel, the Overview card and the
   sidebar chip, and it briefly grew a wording of its own -- "What your photos
   show" for what every other screen calls Search by description.

   They ride on the archive the picker already handed us, so they are on screen
   at the first paint rather than fetched and filled in afterwards. */
export function liveRankings() {
  return (S.arch && S.arch.ways) || [];
}
export function rankingFor(kind) { return liveRankings().find(r => r.id === kind); }

export async function renderPhotos(m) {
  const gen = S.nav;
  const restored = !!(S.grid && Array.isArray(S.grid.pages));
  const g = restored ? S.grid : newGrid("media");
  S.grid = g;
  /* Neither search feature unlocks a nav section -- they are all this one box
     -- so what each of them adds is decided here rather than by dropping a
     section, and the box says what it can actually do.

     The name ranking is unconditional, and now really is: matching what you
     type against file names needs no index, no model and no feature, so it is
     built for every archive and runs on every search. It used to be the
     *fallback*, reached only when there was no description index, which meant
     switching Search by description on quietly took it away. */
  const live = liveRankings();
  const runs = kind => live.some(r => r.id === kind);
  S.nameGrid = restored && S.nameGrid ? S.nameGrid : newGrid("name");
  S.textGrid = runs("text") ? (restored && S.textGrid ? S.textGrid : newGrid("text")) : null;
  refreshGallery();
  /* The placeholder is an invitation, not a label, which is why it is the one
     line here that does not take the features' own names. Stringing them
     together grants the box a whole line of prose -- "Search by filename, by
     extracted text, or by description" -- and the panel directly below already
     lists them properly, one row each with what it matches. So the box says the
     short true thing and leaves the naming to the rows.

     The one-way archive is the exception, since there the placeholder is the
     only thing on the screen that can say what the box will do. */
  const placeholder = live.length === 1
    ? "Search your library by filename"
    : "Search your library";
  const blurb = "Everything in this archive. Narrow it with the filters, or type and Trove will " +
    (live.length === 1 ? "match it against your filenames." : "look in every place it can.");
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
      <div class="library-toolbar">
        <div class="filterbar" id="filterbar" aria-busy="true">${FILTER_SKELETON}</div>
        <div class="chips">
          <select class="fsel sort-sel" id="f-sort" aria-label="Sort media" onchange="applySort()"></select>
          <span class="muted" id="gridcount"></span>
        </div>
      </div>
    </div>
    <section class="search-ways" id="search-ways" hidden></section>
    <div class="results-back" id="results-back" hidden></div>
    <p class="nothing-line" id="nothing-line" hidden></p>
    ${resultsGroup("name", GRID_IDS.name)}
    ${runs("text") ? resultsGroup("text", GRID_IDS.text) : ""}
    ${resultsGroup("media", GRID_IDS.media)}`;
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
  if (runs("media")) composer?.addEventListener("input", warmLocalTranslator, { once: true });
  // Everything that needs nothing from the server goes first, and the grid's
  // own fetch is started here rather than after the filters land. The filter
  // options are needed by the filter bar alone -- the grid asks for the pages
  // the *stored* filters describe -- so awaiting them before loading the grid
  // only added one slow request's time to the other, and left the whole screen
  // blank for the sum. Both requests are now in flight together.
  const filters = buildFilterBar();
  // The panel that says what can be searched here. Drawn for every archive,
  // including the one whose only way is file names: "one way in this archive"
  // is the honest answer, and a screen that says nothing at all is what sent
  // people looking for a search that was there all along.
  renderSearchWays();
  renderSortOptions(g);
  renderActiveQuery(g);
  if (restored) {
    // Only the composer: the rest of the controls do not exist yet.
    restoreLibraryComposer(g);
    // Labels before pages, because a preview is measured off the laid-out grid
    // and a group starts its life hidden. Measuring one that is still
    // `display: none` reads no columns at all.
    renderGroupLabels();
    activeGrids().forEach(grid => {
      renderGridPages(grid);
      const count = document.getElementById(grid.ids.count);
      if (count && grid.total != null) count.textContent = gridCountLabel(grid);
    });
  }
  watchPreviewWidth();
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
export function setupGridInfiniteScroll(g) {
  const top = document.getElementById(g.ids.top);
  const bottom = document.getElementById(g.ids.bottom);
  if (g.observer) { g.observer.disconnect(); g.observer = null; }
  // A previewed group does not observe its own sentinels at all. Leaving them
  // observed is what made the second group unreachable: its heading was always
  // one screen further down than it had been a moment ago.
  if (!top || !bottom || !gridPagesFreely(g)) return;
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
/* A preview is measured in rows, and how many results a row holds is a fact
   about the window. So it has to be redrawn when that changes: widen the window
   and the last row is left ragged -- which is precisely how a grid says "that
   is all of them" -- while the button under it offers a number of results the
   rows above no longer bear out.

   Watched on `#main` rather than on the window because collapsing the nav
   changes the width of these grids without resizing anything. `#main` outlives
   every section (the router only ever replaces its children), so this is
   attached once and left, and the callback asks whether Browse is on screen. */
let previewWidthObserver = null;
function watchPreviewWidth() {
  const main = document.getElementById("main");
  if (!main || previewWidthObserver) return;
  previewWidthObserver = new ResizeObserver(() => {
    if (ACTIVE_SECTION !== "library" || !S.grid) return;
    activeGrids().forEach(g => {
      if (!previewing(g)) return;
      const grid = document.getElementById(g.ids.grid);
      // Only a change in how many fit is worth a repaint: a ResizeObserver
      // fires on every pixel of a drag, and on being attached.
      if (grid && previewCount(grid) !== g.previewCap) renderGridPages(g);
    });
  });
  previewWidthObserver.observe(main);
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
  parts.push(groupFilterHTML("f", "pets", f.pets || []));
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
  // "Best match" needs something ranked to be the best of. An archive whose
  // only ways are file names -- a filter on the dated listing -- has nothing to
  // order by relevance, however much it finds.
  const opts = g.query && mediaRanksQueries()
    ? [["", "Best match"], ["newest", "Newest first"], ["oldest", "Oldest first"]]
    : [["", "Newest first"], ["oldest", "Oldest first"]];
  sel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  // Leaving a search collapses "newest" onto the default, which now means
  // the same thing, so the grid keeps the order the user was looking at.
  if (!opts.some(([v]) => v === g.sort)) g.sort = "";
  sel.value = g.sort;
}
export function applySort() {
  S.grid.sort = selVal("f-sort");
  scrollResultsToTop();   // a different order is a different list to read
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
  setGroupChecks("f", "pets", g.pets || []);
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  updateGroupFilterLabel("f", "pets", S.filterOpts.pets || []);
  updateClearBtn();
}
export function onYearChange() {
  const y = document.getElementById("f-year").value;
  setLibraryMonthOptions(y);
  applyFilters();
}
export function selVal(id) { const e = document.getElementById(id); return e ? e.value : ""; }
/* The words each kind of group filter uses. One widget, two vocabularies:
   filtering by pet asks the same question of the same shape of data, and
   saying "Anyone" over a list of dogs would be the giveaway that it was
   People's control wearing a different hat. */
const GROUP_FILTERS = {
  people: {
    none: "Anyone", empty: "No people named yet",
    enable: "Name people in People to enable this filter",
    hint: "Selecting more than one person shows media containing everyone selected.",
    together: n => `${n} people together`, all: "Only media containing all selected people",
    // Written out per kind, not built from the kind, so tools/dev/check_handlers.py
    // can still see which function each control calls -- it reads the source,
    // and an interpolated name is invisible to it.
    attr: prefix => `onchange="onPeopleFilterChange('${prefix}')"`,
  },
  pets: {
    none: "Any pet", empty: "No pets named yet",
    enable: "Name pets in Pets to enable this filter",
    hint: "Selecting more than one pet shows media containing all of them.",
    together: n => `${n} pets together`, all: "Only media containing all selected pets",
    attr: prefix => `onchange="onPetsFilterChange('${prefix}')"`,
  },
};
export function groupFilterHTML(prefix, kind, items) {
  const words = GROUP_FILTERS[kind];
  if (!items.length)
    return `<span class="fsel filter-placeholder" title="${words.enable}">${words.empty}</span>`;
  return `<details class="multi-filter" id="${prefix}-${kind}-filter">
    <summary class="fsel"><span id="${prefix}-${kind}-label">${words.none}</span></summary>
    <div class="multi-menu">${items.map(p => `<label class="multi-option">
      <input type="checkbox" value="${p.id}" ${words.attr(prefix)}><span>${esc(p.name)}</span>
    </label>`).join("")}
    <div class="multi-help">${words.hint}</div></div>
  </details>`;
}
export function setGroupChecks(prefix, kind, ids) {
  const chosen = new Set(ids.map(String));
  document.querySelectorAll(`#${prefix}-${kind}-filter input[type="checkbox"]`)
    .forEach(input => input.checked = chosen.has(input.value));
}
export function checkedGroups(prefix, kind) {
  return [...document.querySelectorAll(`#${prefix}-${kind}-filter input:checked`)].map(e => e.value);
}
export function clearGroupChecks(prefix, kind) {
  document.querySelectorAll(`#${prefix}-${kind}-filter input:checked`).forEach(e => e.checked = false);
}
export function updateGroupFilterLabel(prefix, kind, items) {
  const words = GROUP_FILTERS[kind];
  const label = document.getElementById(`${prefix}-${kind}-label`); if (!label) return;
  const ids = checkedGroups(prefix, kind),
    names = ids.map(id => (items.find(p => String(p.id) === id) || {}).name).filter(Boolean);
  label.textContent = !names.length ? words.none : names.length === 1 ? names[0] :
    names.length === 2 ? `${names[0]} + ${names[1]}` : words.together(names.length);
  label.closest("summary").title = names.length > 1 ? words.all : names.join("");
}
// The People-shaped calls the timeline and the grid already make.
export const peopleFilterHTML = (prefix, people) => groupFilterHTML(prefix, "people", people);
export const checkedPeople = prefix => checkedGroups(prefix, "people");
export const clearPeopleChecks = prefix => clearGroupChecks(prefix, "people");
export const updatePeopleFilterLabel = (prefix, people) =>
  updateGroupFilterLabel(prefix, "people", people);
export function onPetsFilterChange(prefix) {
  if (prefix === "tl") applyTimelineFilters(); else applyFilters();
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
  g.pets = checkedGroups("f", "pets");
  g.place = selVal("f-place");
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  updateGroupFilterLabel("f", "pets", S.filterOpts.pets || []);
  updateClearBtn();
  scrollResultsToTop();   // a narrower list is a different list to read
  reloadGrids();
}
export function resetGridResults(g) {
  g.offset = 0; g.loaded = 0; g.total = null; g.error = ""; g.doneDown = false; g.doneUp = true; g.gen++;
  g.pages = []; g.anchor = null; g.savedScrollTop = 0; g.previewCap = 0;
  // Emptied before the gallery is rebuilt, because the gallery is read back off
  // these tiles: rebuilding it first would hand the viewer the very results
  // this line is about to remove.
  const grid = document.getElementById(g.ids.grid); if (grid) grid.replaceChildren();
  const more = document.getElementById(g.ids.more); if (more) more.replaceChildren();
  refreshGallery();
  const count = document.getElementById(g.ids.count); if (count) count.textContent = "";
}
/* Back to the top of the results, for the callers that mean it.

   This used to be the last line of resetGridResults, which made emptying a
   group's pages and moving the reader one act. They are not one act: a new
   search or a changed filter replaces what you are reading and belongs at the
   top, while widening the relevance cut only adds results BELOW the ones
   already on screen and has to leave you where you were. Same distinction the
   search writes out for `S.onlyWay` (search.js) -- narrowing what you are
   reading is not a reason to stop reading it. */
export function scrollResultsToTop() {
  const main = document.getElementById("main"); if (main) main.scrollTop = 0;
}
/* Whether the media grid can rank a typed query at all.

   It is the one grid whose job depends on the answer: with Search by
   description on it ranks the query, so "best match" is an order and the
   relevance cuts have something to widen; without it the grid has nothing to
   say about a query and stays out of the results entirely, since file names now
   have a group of their own rather than borrowing this one. */
export function mediaRanksQueries() {
  return archiveHasFeature(S.arch, "semantic");
}
export function clearFilters() {
  ["f-year", "f-type", "f-place"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
  clearPeopleChecks("f");
  clearGroupChecks("f", "pets");
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
  if (b) b.style.display = (g.year || g.month || g.type || g.people.length
    || (g.pets || []).length || g.place)
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
  // A search that found nothing hides its whole group and reports on the
  // collapsed line above instead, so the only messages left to paint here are
  // the ones no line can carry: the plain listing filtered down to nothing, and
  // the ranking you are reading on its own, where that line is not drawn and a
  // filter has since emptied what you came in to read.
  if (g.query && !g.error && S.onlyWay !== g.kind) return;
  const message = g.error ? esc(g.error)
    : g.query ? "No matches under these filters." : "No media matches these filters.";
  grid.innerHTML =
    `<div class="muted" style="grid-column:1/-1;padding:40px;text-align:center">${message}</div>`;
}
export function renderGridPages(g, anchor = null) {
  const grid = document.getElementById(g.ids.grid); if (!grid) return;
  grid.replaceChildren();
  // Read after the grid is emptied and before it is filled: `auto-fill` lays out
  // its tracks from the width available whether or not anything is in them, so
  // this is the count the tiles are about to be placed into.
  const preview = previewing(g);
  const cap = preview ? previewCount(grid) : Infinity;
  g.previewCap = preview ? cap : 0;
  let drawn = 0;
  let lastHeading = null;
  // Which readers can produce a hit in the text group here. With only one of
  // them on, a badge saying so on every tile repeats the heading above it and
  // spends the caption -- which is the file's name -- to say nothing.
  const textWay = g.kind === "text" ? rankingFor("text") : null;
  const mixedReaders = !!textWay && textWay.readers.length > 1;
  const tokens = g.kind === "name" ? nameTokens(g.query) : [];
  g.pages.forEach(page => page.items.forEach((item, itemOffset) => {
    if (drawn >= cap) return;
    // Month headings belong to the listing and nothing else. A group of results
    // already carries a heading saying which way found them, and a second tier
    // of headings under it -- "Unknown date" in 21px over a single tile -- is
    // the listing's furniture in a place that is not the listing. Nor in a
    // preview, where a month name over the two rows on show would read as that
    // month being the whole of the answer.
    if (g.kind === "media" && !preview && (!g.query || g.sort)) {
      const heading = item.date ? (item.date.slice(0, 7) || "Unknown date") : "Unknown date";
      if (lastHeading !== heading) {
        const h = document.createElement("div"); h.className = "date-heading";
        h.textContent = item.date ? fmtDate(item.date.slice(0, 7)) : "Unknown date";
        grid.appendChild(h); lastHeading = heading;
      }
    }
    grid.appendChild(
      g.kind === "text" ? textTile(item, mixedReaders)
        : g.kind === "name" ? nameTile(item, tokens)
          : tile(item, page.offset + itemOffset, "name"));
    drawn++;
  }));
  // `loaded` stays what was fetched rather than what was drawn: it is the
  // paging window's own bookkeeping, and the empty state below reads it to tell
  // "this ranking is empty" from "this ranking is showing you two rows of it".
  g.loaded = g.pages.reduce((n, page) => n + page.items.length, 0);
  renderGroupMore(g, drawn);
  refreshGallery();
  renderGridEmptyState(g, grid);
  if (anchor) restoreLibraryAnchor(anchor);
}
/* Whether a grid has anything to fetch right now.

   The media grid is the one that does two jobs: with nothing typed it is the
   plain dated listing, and with a query it is the description ranking. So it
   runs always while browsing, and while searching only where there is a
   description index to rank against -- otherwise it would answer a search with
   the entire library.

   The other two are rankings and nothing else: no query, no request. */
export function gridAnswers(g) {
  if (g.kind === "media") return !g.query || mediaRanksQueries();
  return !!g.query;
}
/* Which endpoint answers this grid, and with what.

   Four cases, one shape: a plain browse, a name search, a text search and a
   description search. Kept together so that a filter added to one is visibly
   absent from the others rather than quietly missing. The text endpoint takes
   no `type` (a hit is whatever the readers opened) and no `top` (there are no
   relevance cuts to widen -- MATCH is the cut); the name search is a filter on
   the plain listing, so it keeps every parameter that listing takes. */
function gridRequest(g, offset) {
  const p = new URLSearchParams({ root: S.arch.id, offset, limit: GRID_PAGE_SIZE });
  if (g.year) p.set("year", g.year);
  if (g.month) p.set("month", g.month);
  g.people.forEach(id => p.append("person", id));
  (g.pets || []).forEach(id => p.append("pet", id));
  if (g.place) p.set("place", g.place);
  if (g.sort) p.set("sort", g.sort);
  if (g.kind === "text") {
    p.append("q", g.query);
    return "/api/browse/text/search?" + p;
  }
  if (g.type) p.set("type", g.type);
  // The name search, which every archive has and which now runs whatever else
  // is switched on. It used to be the *fallback* -- reached only when there was
  // no description index -- so turning on Search by description silently took
  // away the ability to find a file by its name, and `IMG_2019` went to the
  // picture model, scored below the relevance floor, and came back empty.
  if (g.kind === "name") {
    p.set("name", g.query);
    return "/api/media?" + p;
  }
  if (g.query) {
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
  return "/api/media?" + p;
}
export async function loadGrid(direction = "append", g = S.grid) {
  const gen = g.gen, grid = document.getElementById(g.ids.grid);
  const sentinel = document.getElementById(direction === "prepend" ? g.ids.top : g.ids.bottom);
  // A ranking with nothing to rank has nothing to fetch: it is not a listing, so
  // no query means an empty group rather than every file. Same for the media
  // grid on an archive that cannot rank a query at all.
  if (!gridAnswers(g)) {
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
    // Labels first: they decide whether this group is on screen at all, and a
    // preview is measured off the laid-out grid -- a group still hidden from
    // before its first result landed has no columns to count.
    renderGroupLabels();
    renderGridPages(g, anchor);
    const gc = document.getElementById(g.ids.count);
    if (gc) gc.textContent = gridCountLabel(g);
    const bottom = document.getElementById(g.ids.bottom);
    // A previewed group has a button under it, not a sentinel: it is not
    // waiting to be scrolled into, and saying so would promise paging that this
    // group is deliberately not doing.
    if (bottom) bottom.textContent =
      (g.doneDown || !gridPagesFreely(g)) ? "" : "Scroll to load more";
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
          ACTIVE_SECTION === "library" && gridPagesFreely(g) &&
          gridSentinelIsNear(direction, g))
          loadGrid(direction, g);
      });
    }
  }
}
