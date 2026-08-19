// Search results: the groups a query comes back as, and how much of each one
// is on screen.
//
// Split from library.js along the same seam its stylesheets were split at
// (library.css / results.css): if it draws something you can use *before*
// searching -- the composer, the filter row, the grid, its paging -- it is next
// door; if it only exists because a query returned, it is here. The two are
// read together often enough that the seam is worth stating, and the split is
// what keeps the screen's furniture and its answers from being one pile.
//
// So this module owns: the group a ranking is drawn into, the heading and count
// over it, the two-row preview and the control that opens one ranking in full,
// the collapsed line naming the ways that found nothing, and what set the
// viewer's arrows are bounded by. library.js owns the grids these render.
//
// The two import each other. That is a cycle, and a deliberate one -- the same
// arrangement library.js and search.js already have. Everything crossing it is
// a hoisted function declaration, which is defined before any of it runs.

import {
  ACTIVE_SECTION, backControl, showSection,
} from "./router.js";
import {
  activeGrids, gridAnswers, loadGrid, rankingFor, renderGridPages, resetGridResults,
  setupGridInfiniteScroll,
} from "./library.js";
import {
  esc,
} from "./dom.js";
import {
  setGallery,
} from "./gallery.js";
import {
  updateWaysCoverage,
} from "./search.js";
import {
  ICONS, S,
} from "./state.js";

/* Why Browse lists fewer files than the archive holds.

   The Overview counts every file it catalogued -- 136 on the fixture -- and its
   "All files" tile is a button straight into this screen, which lists 6. Both
   numbers are right: a redundant copy is still a catalogued file, and Browse
   deliberately shows one of each thing rather than 131 of the same photograph.
   Nothing said so anywhere, and the gap is one click wide.

   So the toolbar's count carries the difference, next to the figure it explains
   and nowhere else. Only while browsing: during a search the number beside it
   counts matches, and how many copies are hidden from the library is not a fact
   about the search.

   The link goes to Duplicates, which is the screen that can actually do
   something about it. */
function hiddenCopiesNote(host, searching) {
  const hidden = !searching && S.dupsum && S.dupsum.duplicates;
  if (!hidden) return;
  host.append(" · ");
  const link = document.createElement("button");
  link.type = "button";
  link.className = "hidden-copies";
  link.textContent = `${hidden.toLocaleString()} ${hidden === 1 ? "copy" : "copies"} hidden`;
  link.dataset.tip = "Redundant copies are kept out of Browse. Open Duplicates.";
  link.onclick = () => showSection("dups");
  host.append(link);
}

/* How much of a ranking the overview shows before you ask for the rest.

   Rows, not a number of results. A fixed count lands mid-row at every window
   width but one, and a half-filled last row is exactly how a grid says "that
   is all of them" -- the opposite of what a preview means. Two of them because
   one row is too thin a sample to judge a ranking by and three pushes the last
   group off the first screen, which is the problem the preview exists to fix. */
const PREVIEW_ROWS = 2;
/* One group of results, with the heading that says which ranking produced it.

   The heading is markup rather than a string set later because it holds the
   ranking's mark, its name and its count, and those are three elements that
   have to line up; `renderGroupLabels` fills the count and hides the group. */
export function resultsGroup(kind, ids) {
  const r = rankingFor(kind);
  // The media group is the plain dated listing until something is typed, so it
  // is on screen from the first paint. Starting it hidden and unhiding it once
  // the first page landed put the whole grid inside a `display:none` container
  // while its thumbnails were being requested, which cost some of them.
  const start = kind === "media" ? ' class="results-group plain"' : ' class="results-group" hidden';
  // ...and it is the listing even on an archive that cannot rank a query at
  // all, which is the one case where the media grid has no *way* behind it. It
  // gets no heading then, because there is no ranking for one to name and the
  // listing never shows one anyway.
  const heading = r
    ? `<h3 class="results-label">
        <span class="ranking-mark" aria-hidden="true">${ICONS[r.icon]}</span>
        <span class="ranking-name">${esc(r.label)}</span>
        <span class="muted" id="${ids.count}"></span>
      </h3>`
    : `<h3 class="results-label"><span class="muted" id="${ids.count}"></span></h3>`;
  return `<section${start} id="group-${kind}">
      ${heading}
      <div class="infinite-status top" id="${ids.top}" aria-hidden="true"></div>
      <div class="grid" id="${ids.grid}"></div>
      <div class="group-more" id="${ids.more}"></div>
      <div class="infinite-status" id="${ids.bottom}" aria-live="polite"></div>
    </section>`;
}
/* Whether this group is one of several being previewed, rather than the one
   ranking the screen is currently given over to.

   Browsing is never a preview: the media grid with nothing typed is the plain
   dated listing, and a listing you can see two rows of is not a listing. */
export function previewing(g) { return !!g.query && !S.onlyWay; }
/* Whether a group may keep loading as you scroll.

   At most one may, and which one is the whole point of the arrangement. Three
   groups stacked in a single scroller, each extending itself as its own bottom
   comes into view, means the second group sits behind the entirety of the first
   and the third is unreachable in practice -- you cannot get to the documents
   without first scrolling through every file whose name matched. So while a
   search is on screen the only group that grows is the one you asked for; the
   plain listing, which is alone on the screen, still grows freely. */
export function gridPagesFreely(g) { return !g.query || S.onlyWay === g.kind; }
/* How many results a preview draws: whole rows of however many fit right now,
   read back off the laid-out grid rather than assumed.

   `repeat(auto-fill, …)` decides the column count from the width available, and
   the groups do not even agree on a column width -- a passage needs a wider
   cell than a thumbnail (results.css) -- so this is a fact about the screen at
   this moment, not a constant this file could hold. */
export function previewCount(grid) {
  const tracks = getComputedStyle(grid).gridTemplateColumns.split(" ");
  const columns = tracks.filter(track => track.endsWith("px")).length;
  // A group with no width to read is one that is not laid out -- hidden, or
  // detached. Clamping to a guess there would hide results; not clamping shows
  // too many, and the next render, with a width, corrects it.
  return columns ? columns * PREVIEW_ROWS : Infinity;
}
/* Leaving the preview for one ranking, and coming back out of it.

   Both are a repaint rather than a reload: every group keeps the pages it has
   already fetched, so going in and coming back costs nothing and the totals
   behind the other headings are still answers to the same search. What actually
   changes is which group is allowed to keep paging (`gridPagesFreely`) and how
   much of each one is drawn -- and both of those are read fresh on every
   render, so switching view is a matter of rendering again. */
function showOnlyWay(kind) {
  const main = document.getElementById("main");
  S.overviewScrollTop = main ? main.scrollTop : 0;
  S.onlyWay = kind;
  renderGroupLabels();
  activeGrids().forEach(g => { renderGridPages(g); setupGridInfiniteScroll(g); });
  /* The one place here a jump to the top is not a reset.

     Every group above the one being opened comes off the screen with this, so
     the ranking you pressed on IS the top afterwards -- holding its heading
     where it was and scrolling to nought are the same instruction, and the
     second is the one that says so. What must not be lost is where you were
     reading, and that is kept above and put back by showAllWays. */
  if (main) main.scrollTop = 0;
}
function showAllWays() {
  S.onlyWay = "";
  renderGroupLabels();
  const refetched = [];
  activeGrids().forEach(g => {
    /* A ranking scrolled deep no longer holds its own first results: the window
       is capped at GRID_MAX_PAGES and the early pages have been dropped off the
       top of it. A preview showing results 481 to 494 of a ranking is not a
       preview of that ranking, so a group that was paged through is put back to
       its first page. Only that group, and only when it actually moved. */
    if (g.pages.length && g.pages[0].offset > 0) {
      resetGridResults(g); refetched.push(loadGrid("append", g));
    } else renderGridPages(g);
    setupGridInfiniteScroll(g);
  });
  /* Back where you were reading before you opened one ranking.

     Waited on rather than done in the next frame, which is what made this fail
     in exactly the case it exists for: a ranking you actually read through is
     one that paged, and a group that paged is emptied and re-fetched above. Its
     results are still in flight one frame later, so the screen is short, the
     position clamps to whatever fits, and the pages then land under a reader
     who has already been moved to the top. */
  const main = document.getElementById("main");
  if (!main) return;
  const restore = () => requestAnimationFrame(() => {
    if (ACTIVE_SECTION === "library" && !S.onlyWay) main.scrollTop = S.overviewScrollTop || 0;
  });
  if (refetched.length) Promise.all(refetched).then(restore);
  else restore();
}
/* The viewer walks whatever is on screen, in the order it is on screen -- so
   with two groups showing it has to be both of them, text first, matching the
   document order.

   Read back off the tiles rather than off the pages behind them, which is the
   only version of "on screen" that stays true now that a group can hold more
   results than it is drawing. Built from the loaded pages, a preview of ten
   would have handed the viewer all hundred and twenty, and arrowing right off
   the last tile would have walked into results with nothing on screen to show
   for them. The DOM already holds the answer, in the order it is displaying it
   -- including which groups are hidden, which is the same question again. */
export function refreshGallery() {
  const main = document.getElementById("main");
  const tiles = main
    ? [...main.querySelectorAll(".results-group:not([hidden]) [data-file-id]")] : [];
  setGallery(tiles.map(tile => Number(tile.dataset.fileId)), gallerySource());
}
/* What the viewer's position readout ends with. One ranking on its own is
   named, because the arrows are bounded by that ranking and not by the search
   -- the same reason a person's photos say whose they are. */
function gallerySource() {
  if (!S.grid || !S.grid.query) return "in Browse";
  const way = S.onlyWay ? rankingFor(S.onlyWay) : null;
  return way ? `in ${wayNoun(way.label)} matches` : "in these results";
}
/* What a group's count reads. "Matches" everywhere a query ran, and files only
   for the plain listing.

   The text group used to count "documents", which stopped being true the day the
   picture half was added: a hit there can be a photographed receipt. Counting
   matches says the same thing about the search without claiming anything about
   what was matched. */
export function gridCountLabel(g) {
  const n = (g.total || 0).toLocaleString();
  if (!g.query && g.kind === "media") return `${n} files`;
  return `${n} match${g.total === 1 ? "" : "es"}`;
}
/* Which groups are on screen, and what the ones that found nothing say.

   Browsing with nothing typed is one grid and no headings at all: a label
   telling you which of one thing you are looking at is noise, and the media
   grid is the plain dated listing rather than a result.

   Searching draws a heading over every ranking that found something, and
   collapses the ones that found nothing into a single line above them. Both
   halves of that matter. A ranking that was searched and came back empty is an
   answer -- "the documents were looked in" is worth knowing, and its absence is
   what used to make an archive with one feature wonder whether the others had
   run. But three empty headings stacked above your photos is worse than the
   missing label it replaced, so they report as one quiet line instead.

   The line goes above the results rather than under them because it qualifies
   them: it says which of the ways this archive has were tried and came back
   with nothing, so what follows can be read as a partial answer. Under a
   thousand photos it is a footnote nobody scrolls to, and the reader who most
   needs it -- the one wondering whether the documents were searched at all --
   is exactly the one who stops at the first screenful. */
export function renderGroupLabels() {
  const searching = !!S.grid.query;
  // Only a search has separate rankings to be reading one of, so browsing can
  // never be inside one however it got there.
  const only = searching ? S.onlyWay : "";
  const empty = [];
  activeGrids().forEach(g => {
    const group = document.getElementById("group-" + g.kind);
    if (!group) return;
    const hits = !!g.total;
    // The media grid with nothing typed is the listing, not a result: it stays
    // on screen whatever it holds -- an archive filtered down to nothing still
    // needs somewhere to say so -- and loses its heading, which is what
    // `.results-group.plain` turns off. Every other group is a ranking, and a
    // ranking with nothing in it has nothing to show.
    const listing = g.kind === "media" && !searching;
    // One ranking open is that ranking and nothing else -- shown even after a
    // filter has emptied it, because then it is the only thing left that can
    // say so, and the collapsed line that would otherwise carry the news is
    // not drawn in here.
    group.hidden = only ? g.kind !== only : (!listing && !hits);
    group.classList.toggle("plain", listing);
    // `rankingFor` can be empty for the media grid on an archive with no
    // description index -- but so is `gridAnswers` there, so a way that was
    // never consulted is never reported as having found nothing. `total` is
    // null until a way has answered, and a way still being waited on has not
    // found nothing.
    const way = rankingFor(g.kind);
    if (searching && g.total != null && !hits && gridAnswers(g) && way) empty.push(way);
  });
  refreshGallery();
  // The toolbar's count is the whole answer, not one group's: with results in
  // three places, a number sitting beside the sort control has to be the total
  // or it is a figure with no label. Inside one ranking the same rule points
  // the other way -- there the whole answer *is* that ranking's.
  const overall = document.getElementById("gridcount");
  if (overall) {
    const grids = activeGrids().filter(g =>
      g.total != null && gridAnswers(g) && (!only || g.kind === only));
    const n = grids.reduce((sum, g) => sum + g.total, 0);
    overall.replaceChildren();
    if (grids.length) {
      overall.append(searching
        ? `${n.toLocaleString()} result${n === 1 ? "" : "s"}`
        : `${n.toLocaleString()} files`);
      hiddenCopiesNote(overall, searching);
    }
  }
  updateWaysCoverage();
  renderResultsBack(only);
  const line = document.getElementById("nothing-line");
  if (!line) return;
  // Not drawn inside a ranking: it is a note about the ways you have set aside,
  // and the way back to them is on screen a line above it.
  line.hidden = !empty.length || !!only;
  if (!empty.length || only) { line.replaceChildren(); return; }
  const anyHits = activeGrids().some(g => g.total);
  line.replaceChildren(document.createTextNode(
    anyHits ? "Nothing found by " : "Searched, with nothing found by "));
  empty.forEach((r, i) => {
    if (i) line.append(document.createTextNode(i === empty.length - 1 ? " or " : ", "));
    const item = document.createElement("span");
    item.className = "nl-item";
    const glyph = document.createElement("span");
    glyph.className = "ranking-mark";
    glyph.setAttribute("aria-hidden", "true");
    glyph.innerHTML = ICONS[r.icon];
    item.append(glyph, document.createTextNode(wayNoun(r.label)));
    line.append(item);
  });
}
/* Every way is called "Search by <what you type against>" (ADR 0021), so the
   line above says that once and names each by the half that tells it from the
   others. Nothing is lowercased and nothing retyped: what is left is the noun
   the feature was named for, minus a prefix the sentence has supplied. */
function wayNoun(label) { return label.replace(/^Search by /, ""); }
/* The way back out of one ranking.

   Just "Back", against the grain of the rest of this screen, which names things
   in full. Every phrase for where it returns to is a word away from "All
   results" on the search's own line, and that segment means something else
   entirely -- how far down its own ranking a description search is willing to
   go. Two controls a few pixels apart, both reading as "show me more", is worse
   than a plain word. The heading beside it says where you are; this says how to
   leave, and the hover text carries the sentence. */
function renderResultsBack(only) {
  const bar = document.getElementById("results-back"); if (!bar) return;
  bar.hidden = !only;
  if (!only) { bar.replaceChildren(); return; }
  // Built once and left alone: every load relabels the groups, and rebuilding
  // this would take the focus off it each time.
  if (bar.firstChild) return;
  // The app's back control, not one of its own. This drew a typed "←" beside
  // the word, which is a different mark at a different weight from the chevron
  // the sidebar and a person's page use for the same act.
  bar.innerHTML = backControl("every way");
  bar.querySelector("button").onclick = showAllWays;
}
/* The way into one, under the two rows the overview shows of it.

   It says the ranking's own total rather than what is left behind the preview.
   The heading two rows above already carries that number, so a second and
   smaller one -- "show the other 2,837" -- is arithmetic the reader has to do
   to satisfy themselves the two agree, and it is the one of the pair that moves
   when the window changes width while the total sits still. */
export function renderGroupMore(g, drawn) {
  const box = document.getElementById(g.ids.more); if (!box) return;
  if (!previewing(g) || !g.total || g.total <= drawn) { box.replaceChildren(); return; }
  const way = rankingFor(g.kind);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "more-btn";
  button.textContent = `Show all ${g.total.toLocaleString()}`;
  if (way) button.dataset.tip = `Every result from ${way.label}, on its own`;
  button.onclick = () => showOnlyWay(g.kind);
  box.replaceChildren(button);
}
