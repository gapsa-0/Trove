// The pieces People and Pets render the same way: the detection status row that
// heads both screens, the incremental card-grid resync both use to update in
// place, and the placeholder for a screen that is not built yet.

import {
  jget,
} from "./api.js";
import {
  esc,
} from "./dom.js";
import {
  S,
} from "./state.js";

async function renderFolders(m) {
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Folders</h2><p>See where the original files in this archive live.</p></div></div><div class="panel"><div class="muted">Reading folders…</div></div>`;
  const res = await jget("/api/folders?root=" + S.arch.id); const folders = res.folders || [];
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Folders</h2><p>See where the original files in this archive live. Nothing here changes them.</p></div></div><div class="panel"><div class="folder-list">${folders.length ? folders.map(f => `<div class="taskcard"><span class="ico">□</span><div class="body"><div class="t">${esc(f.path)}</div></div><div class="num">${f.count.toLocaleString()}</div></div>`).join("") : '<div class="muted">No catalogued files yet.</div>'}</div></div>`;
}
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
export function renderSoon(m, id) {
  const S2 = {
    places: ["📍", "Places", "Reverse-geocode coordinates into place names and browse by location."],
    situations: ["🔎", "Situations", "Search by content: “beach sunset”, “birthday”, using local image embeddings."]
  }[id];
  m.innerHTML = `<h2 class="sec">${S2[1]}</h2><div class="soonbox"><div class="big">${S2[0]}</div>
    <p>${S2[2]}</p><p class="muted">Coming in a later phase.</p></div>`;
}
