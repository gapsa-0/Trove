// The pieces People and Pets render the same way: the detection status row that
// heads both screens, the incremental card-grid resync both use to update in
// place, and the fallback for a hash that names no screen at all.

import {
  esc,
} from "./dom.js";

// Shared status line for the fused people+pets `detect` stage. `failed`
// is the People-only retry-cooldown message; Pets currently always passes
// null. First-run addendum applies to both since it's the same model.
export function detectStatusRow(sum, failed) {
  if (failed) {
    return `<div class="d pending"><span class="dot pending"></span>Detection paused; retrying automatically. ${esc(failed)}</div>`;
  }
  if (sum.unscanned > 0) {
    const first = sum.scanned === 0;
    const note = `${sum.unscanned.toLocaleString()} unique photo${sum.unscanned === 1 ? "" : "s"} pending; detection runs automatically${first ? " (first run fetches a ~38 MB face model once)" : ""}.`;
    return `<div class="d pending"><span class="dot pending"></span>${note}</div>`;
  }
  return `<div class="d ok"><span class="dot ok"></span>All unique photos scanned.</div>`;
}
/* Patch a card grid to match a freshly fetched head of its list, instead of
   tearing the grid down and rebuilding it -- scroll position, the pages the
   infinite list has already loaded and any half-finished review all
   survive. Cards carry their identity in dataset.syncKey, set by whichever
   factory built them (the initial render appends cards directly, so the key
   can't be assigned here): survivors are updated in place and moved to
   their new position, newcomers are inserted at theirs, and cards whose
   item is gone are dropped -- but only when `complete` says the fetch
   covered the whole list, since a full page says nothing about the items
   sitting past it. `update` returns false to leave a card alone (mid-edit).
   Returns the number of cards now in the grid. */
export function syncCardGrid(grid, items, { keyOf, make, update, complete, empty }) {
  // Placeholders ("nothing here yet") aren't cards; drop them so the
  // positional insert below lines up with the item list.
  for (const el of [...grid.children]) if (!el.dataset.syncKey) el.remove();
  const existing = new Map();
  for (const el of grid.children) existing.set(el.dataset.syncKey, el);
  items.forEach((item, i) => {
    const key = String(keyOf(item));
    let el = existing.get(key);
    if (el) { existing.delete(key); if (update && !update(el, item)) return; }
    else el = make(item);
    const at = grid.children[i];
    if (at !== el) grid.insertBefore(el, at || null);
  });
  if (complete) existing.forEach(el => el.remove());
  const cards = grid.children.length;
  if (!cards && complete && empty) grid.innerHTML = empty;
  return cards;
}
// The fallback for a hash naming a section RENDERERS does not have: a stale
// bookmark, a typo, a link from an older build. It used to hold "coming in a
// later phase" copy for two screens -- and both of them shipped, `places` into
// RENDERERS and `situations` as semantic search. That left it reachable only
// for ids its lookup had no entry for, which it then indexed unguarded: every
// route that actually reached it threw a TypeError instead of rendering.
export function renderUnknownSection(m, id) {
  m.innerHTML = `<h2 class="sec">Not found</h2><div class="soonbox"><div class="big">🧭</div>
    <p>There is no “${esc(id)}” screen in this version of Trove.</p>
    <p class="muted">Pick a section from the sidebar.</p></div>`;
}

/* Up to 4 thumbnails as a 2x2 collage, one filling the square. Used by a
   person's card and a pet's, which differ only in which endpoint draws a
   crop -- so `endpoint` is the argument rather than two near-identical copies.

   The first id leads, and the caller orders them: both screens put the cover
   the user chose at the front, so the card opens on the picture they picked
   and fills the rest with the sharpest of what is left. */
export function thumbCollage(ids, endpoint) {
  ids = (ids || []).filter(Boolean).slice(0, 4);
  const img = id =>
    `<img src="${endpoint}/${id}" loading="lazy" draggable="false" onerror="this.style.visibility='hidden'">`;
  if (ids.length <= 1) {
    // draggable=false: these sit inside merge-draggable cards and would
    // otherwise hijack the card drag with their own payload.
    return ids[0]
      ? img(ids[0]).replace("<img ", '<img class="face" ')
      : '<div class="face"></div>';
  }
  let cells = "";
  for (let i = 0; i < 4; i++) cells += ids[i] ? img(ids[i]) : '<div class="cempty"></div>';
  return `<div class="facecollage">${cells}</div>`;
}
/* Open the name editor on the card beside this one.

   What Tab means on a grid of unnamed groups: the point of naming one is
   usually to name the next, and reaching for the mouse between every name is
   most of the work. Found by key rather than held as a reference, because the
   card that was just saved is replaced and the grid reconciled against the
   server before this runs -- the node the caller was holding is gone.

   Clicking the name rather than calling the editor directly, so every grid
   keeps its own idea of what opening one means, and the next card is scrolled
   just far enough to be seen: `nearest` leaves the grid alone when it already
   is. */
export function editNeighbourName(gridId, key, step) {
  const grid = document.getElementById(gridId); if (!grid || !step) return;
  const cards = [...grid.children];
  const at = cards.findIndex(card => card.dataset.syncKey === String(key));
  const next = at < 0 ? null : cards[at + step];
  const name = next && next.querySelector(".pname");
  if (!name) return;
  next.scrollIntoView({ block: "nearest" });
  name.click();
}
