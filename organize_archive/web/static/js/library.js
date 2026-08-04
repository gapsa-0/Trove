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
export async function renderPhotos(m) {
  const gen = S.nav;
  const restored = !!(S.grid && Array.isArray(S.grid.pages));
  const g = restored ? S.grid : {
    offset: 0, loaded: 0, gen: 0, year: "", month: "", type: "", people: [], inferredPeople: [],
    place: "", rawQuery: "", searchedQuery: "", query: "",
    expandedQuery: "", sort: "", error: "", topMatchesOnly: true,
    total: null, doneDown: false, doneUp: true, loadingGen: null, observer: null, pages: [],
    anchor: null, savedScrollTop: 0,
  };
  S.grid = g;
  S.gallery = g.pages.flatMap(page => page.items.map(item => item.id));
  // Search by description is a feature of this screen, not a screen of its
  // own, so it is the one feature that cannot be switched off by dropping a
  // nav section. Without this the composer rendered on every archive, and an
  // archive that declined the feature was invited to search an index whose
  // stage the scheduler will never start -- then told "no files searchable by
  // description yet", promising work that was never coming.
  const searchable = archiveHasFeature(S.arch, "semantic");
  m.innerHTML = `<div class="pagehead">
      <div><h2 class="sec">Browse</h2>
      <p>${searchable
    ? "Look through every item, by filter or by description."
    : "Look through every item, with filters that work together."}</p></div>
    </div>
    <div class="library-controls">
      ${searchable ? `<form class="library-search" onsubmit="return semanticSubmit(event)">
        <div class="semantic-composer" id="semantic-q" contenteditable="true" role="textbox"
          aria-label="Search your library by description" data-placeholder="Search your library, describe anything"
          spellcheck="true" oninput="onSemanticComposerInput()" onkeydown="onSemanticComposerKeydown(event)"
          onpaste="onSemanticComposerPaste(event)"></div>
        <button class="btn" type="submit">Search</button>
      </form>
      <div class="active-query" id="active-query" aria-live="polite" hidden></div>
      <div class="search-reach" id="search-reach" aria-live="polite" hidden></div>` : ""}
      <div class="library-toolbar">
        <div class="filterbar" id="filterbar"></div>
        <div class="chips">
          <select class="fsel sort-sel" id="f-sort" aria-label="Sort media" onchange="applySort()"></select>
          <span class="muted" id="gridcount"></span>
        </div>
      </div>
    </div>
    <div class="infinite-status top" id="grid-top-sentinel" aria-hidden="true"></div>
    <div class="grid" id="grid"></div>
    <div class="infinite-status" id="grid-sentinel" aria-live="polite"></div>`;
  // Absent on an archive that does not run Search by description, where the
  // rest of this screen — grid, filters, sort, paging — is unaffected.
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
  composer?.addEventListener("input", warmLocalTranslator, { once: true });
  await buildFilterBar();
  if (gen !== S.nav) return;
  renderSearchReach();
  renderSortOptions(g);
  renderActiveQuery(g);
  if (restored) {
    restoreLibraryControls(g);
    renderGridPages(g);
    const count = document.getElementById("gridcount");
    if (count && g.total != null) count.textContent = gridCountLabel(g);
  }
  setupGridInfiniteScroll(g);
  if (restored && g.pages.length) {
    requestAnimationFrame(() => {
      if (ACTIVE_SECTION !== "library" || S.grid !== g) return;
      if (!restoreLibraryAnchor(g.anchor))
        document.getElementById("main").scrollTop = g.savedScrollTop || 0;
    });
  } else loadGrid();
}
function setupGridInfiniteScroll(g) {
  const top = document.getElementById("grid-top-sentinel");
  const bottom = document.getElementById("grid-sentinel");
  if (!top || !bottom) return;
  if (g.observer) g.observer.disconnect();
  g.observer = new IntersectionObserver(entries => {
    if (S.grid !== g || ACTIVE_SECTION !== "library") return;
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      if (entry.target === top && !g.doneUp) loadGrid("prepend");
      else if (entry.target === bottom && !g.doneDown) loadGrid("append");
    });
  }, { root: document.getElementById("main"), rootMargin: "600px 0px" });
  g.observer.observe(top);
  g.observer.observe(bottom);
}
function gridSentinelIsNear(direction) {
  const sentinel = document.getElementById(
    direction === "prepend" ? "grid-top-sentinel" : "grid-sentinel");
  const main = document.getElementById("main");
  if (!sentinel || !main) return false;
  const sr = sentinel.getBoundingClientRect(), mr = main.getBoundingClientRect();
  return direction === "prepend" ? sr.bottom >= mr.top - 600 : sr.top <= mr.bottom + 600;
}
async function buildFilterBar() {
  const gen = S.nav;
  const f = await jget("/api/browse/filters?root=" + S.arch.id);
  if (gen !== S.nav) return;
  S.filterOpts = f;
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
  const opts = g.query
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
  return g.query ? `${n} match${g.total === 1 ? "" : "es"}` : `${n} files`;
}
export function applySort() {
  const g = S.grid;
  g.sort = selVal("f-sort");
  resetGridResults(g);
  loadGrid();
}
function restoreLibraryControls(g) {
  const year = document.getElementById("f-year"); if (year) year.value = g.year || "";
  setLibraryMonthOptions(g.year, (g.month || "").slice(5, 7));
  const type = document.getElementById("f-type"); if (type) type.value = g.type || "";
  const place = document.getElementById("f-place"); if (place) place.value = g.place || "";
  setPeopleChecks("f", g.people || []);
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  const composer = document.getElementById("semantic-q");
  if (composer) { composer.textContent = g.rawQuery || ""; renderSemanticComposer(true); }
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
  resetGridResults(g);
  updateClearBtn();
  loadGrid();
}
export function resetGridResults(g) {
  g.offset = 0; g.loaded = 0; g.total = null; g.error = ""; g.doneDown = false; g.doneUp = true; g.gen++;
  g.pages = []; g.anchor = null; g.savedScrollTop = 0;
  S.gallery = [];
  const grid = document.getElementById("grid"); if (grid) grid.replaceChildren();
  const count = document.getElementById("gridcount"); if (count) count.textContent = "";
  const main = document.getElementById("main"); if (main) main.scrollTop = 0;
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
  grid.innerHTML = `<div class="muted" style="grid-column:1/-1;padding:40px;text-align:center">${
    g.error ? esc(g.error) : "No media matches these filters."}</div>`;
}
function renderGridPages(g, anchor = null) {
  const grid = document.getElementById("grid"); if (!grid) return;
  grid.replaceChildren();
  let lastHeading = null;
  g.pages.forEach(page => page.items.forEach((item, itemOffset) => {
    // Month headings only make sense while the grid is in date order.
    if (!g.query || g.sort) {
      const heading = item.date ? (item.date.slice(0, 7) || "Unknown date") : "Unknown date";
      if (lastHeading !== heading) {
        const h = document.createElement("div"); h.className = "date-heading";
        h.textContent = item.date ? fmtDate(item.date.slice(0, 7)) : "Unknown date";
        grid.appendChild(h); lastHeading = heading;
      }
    }
    grid.appendChild(tile(item, page.offset + itemOffset));
  }));
  S.gallery = g.pages.flatMap(page => page.items.map(item => item.id));
  g.loaded = S.gallery.length;
  renderGridEmptyState(g, grid);
  if (anchor) restoreLibraryAnchor(anchor);
}
export async function loadGrid(direction = "append") {
  const g = S.grid, gen = g.gen, grid = document.getElementById("grid");
  const sentinel = document.getElementById(
    direction === "prepend" ? "grid-top-sentinel" : "grid-sentinel");
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
    const p = new URLSearchParams({
      root: S.arch.id, offset: requestedOffset, limit: GRID_PAGE_SIZE,
    });
    if (g.type) p.set("type", g.type); if (g.year) p.set("year", g.year);
    if (g.month) p.set("month", g.month);
    g.people.forEach(id => p.append("person", id)); if (g.place) p.set("place", g.place);
    if (g.sort) p.set("sort", g.sort);
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
    }
    const endpoint = g.query ? "/api/browse/semantic/search?" : "/api/media?";
    const res = await jget(endpoint + p);
    // Bail if a newer filter change (or a section switch) superseded this fetch
    // while it was in flight, so a slow response can't paint stale tiles.
    if (S.grid !== g || g.gen !== gen) return;
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
    const gc = document.getElementById("gridcount");
    if (gc) gc.textContent = gridCountLabel(g);
    const bottom = document.getElementById("grid-sentinel");
    if (bottom) bottom.textContent = g.doneDown ? "" : "Scroll to load more";
  } catch {
    failed = true;
    if (S.grid === g && g.gen === gen && sentinel)
      sentinel.textContent = "Couldn’t load more files. Scroll away and back to retry.";
  } finally {
    if (g.loadingGen === gen) {
      g.loadingGen = null;
      // An initial page may not be tall enough to move the sentinel outside
      // the observer margin. Keep filling until the user has something to
      // scroll, without requiring an intersection state change.
      requestAnimationFrame(() => {
        const done = direction === "prepend" ? g.doneUp : g.doneDown;
        if (!failed && S.grid === g && g.gen === gen && !done &&
          ACTIVE_SECTION === "library" && gridSentinelIsNear(direction)) loadGrid(direction);
      });
    }
  }
}
export function tile(it, resultIndex = null) {
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
  // `indexed` is absent on description-search results -- every hit there is
  // indexed by definition, so the pip would mark all of them and say
  // nothing. Undefined simply renders no pip, which is the wanted result.
  cap.innerHTML = `<span>${(it.date || "").slice(0, 10)}</span><span class="cap-marks">` +
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
