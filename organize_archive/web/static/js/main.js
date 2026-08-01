// The Trove frontend, entry point.
//
// Loaded as `<script type="module">`, so everything in here is module-scoped:
// nothing lands on `window` unless the export block at the bottom puts it there.
// That block is the one thing to read before changing anything above it.

import {
  esc,
} from "./dom.js";
import {
  ARCHIVES, addArchiveFromForm, loadPicker, openArchive,
} from "./picker.js";
import {
  stopGlobalStatus,
} from "./status.js";
import {
  applyFilters, applySort, clearFilters, onPeopleFilterChange, onYearChange, renderPhotos,
} from "./library.js";
import {
  onSemanticComposerInput, onSemanticComposerKeydown, onSemanticComposerPaste,
  renderSearchReach, semanticSubmit,
} from "./search.js";
import {
  INFINITE_LIST_KEYS,
} from "./infinite.js";
import {
  MITEM, addPersonPicker, addPetPicker, closeModal, closePick, editDate, editPlace, newPlace,
  onAddPerson, onAddPet, onPlaceSelect, openItem, reassignFace, removeManualPerson,
  removeManualPet, renderInfo, saveDate, saveNewPlace, syncPickerMapTiles,
} from "./item.js";
import {
  renamePet, renderPets, startPetPoll,
} from "./pets.js";
import {
  answerSuggest, backToPeople, editPersonName, hidePerson, renderFaces, startFacePoll,
} from "./people.js";
import {
  mergeAskCancel, undoMerge,
} from "./merge.js";
import {
  MAP, closePlaceCluster, disposeMap, drawMap, editClusterName, renderMap, setMapView,
  syncPlacesMapTiles,
} from "./places.js";
import {
  applyTimelineFilters, clearTimelineFilters, onTimelineYearChange, renderTimeline,
} from "./timeline.js";
import {
  renderOverview, setStorageMetric, startPoll, stopPoll, togglePipelinePause,
  toggleStagePause,
} from "./overview.js";
import {
  renderDedup,
} from "./dups.js";
import {
  ICONS, S, SECTIONS,
} from "./state.js";
import {
  jget, jpost,
} from "./api.js";

// Checkbox filter menus behave like native popovers: only one stays open, and
// clicking elsewhere or pressing Escape dismisses it without changing choices.
document.addEventListener("pointerdown", event => {
  document.querySelectorAll(".multi-filter[open]").forEach(menu => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape")
    document.querySelectorAll(".multi-filter[open]").forEach(menu => menu.removeAttribute("open"));
});
document.addEventListener("toggle", event => {
  if (event.target.matches && event.target.matches(".multi-filter[open]"))
    document.querySelectorAll(".multi-filter[open]").forEach(menu => {
      if (menu !== event.target) menu.removeAttribute("open");
    });
}, true);

export function currentTheme() { return document.documentElement.dataset.theme === "dark" ? "dark" : "light"; }
function syncThemeControl() {
  const dark = currentTheme() === "dark";
  // Gear buttons (settings) share the .theme-toggle look but carry no theme
  // icon/label, so skip anything without a .theme-icon so they don't break here.
  document.querySelectorAll(".theme-toggle,.appearance-fab").forEach(button => {
    const icon = button.querySelector(".theme-icon");
    if (!icon) return;
    icon.innerHTML = dark ? ICONS.sun : ICONS.moon;
    const label = button.querySelector(".theme-label");
    if (label) label.textContent = dark ? "Light appearance" : "Dark appearance";
    button.title = dark ? "Use light appearance" : "Use dark appearance";
  });
  document.querySelectorAll(".gear-icon").forEach(el => { el.innerHTML = ICONS.settings; });
}
function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("archiveTheme", next);
  document.querySelector('meta[name="theme-color"]').content = next === "dark" ? "#101014" : "#f5f5f7";
  syncThemeControl();
  syncMapTiles();
}

/* ---------- settings drawer (app-wide config) ---------- */
// Nothing in here is user-configurable any more: semantic search stopped
// needing an API key when the embedding model moved on-device, so the drawer
// is appearance plus a statement of what runs where.
function openSettings() {
  const d = document.getElementById("settings-drawer"), b = document.getElementById("drawer-backdrop");
  b.classList.add("open"); d.classList.add("open"); d.setAttribute("aria-hidden", "false");
}
function closeSettings() {
  const d = document.getElementById("settings-drawer"), b = document.getElementById("drawer-backdrop");
  b.classList.remove("open"); d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
}

/* ---------- picker ---------- */

/* ---------- archive shell ---------- */

function applyHash() {
  const m = (location.hash || "").match(/#\/archive\/(\d+)\/(\w+)/);
  if (m) { const a = ARCHIVES.find(x => x.id == +m[1]); if (a) { openArchive(a, m[2]); return true; } }
  return false;
}
window.addEventListener("hashchange", () => {
  const match = (location.hash || "").match(/#\/archive\/(\d+)\/(\w+)/);
  if (!match) return;
  const archive = ARCHIVES.find(item => item.id === Number(match[1]));
  if (!archive) return;
  if (S.arch && S.arch.id === archive.id) showSection(match[2]);
  else openArchive(archive, match[2]);
});
function toPicker() {
  if (S.arch) jpost("/api/archive/close", { root_id: S.arch.id });
  stopPoll(); stopGlobalStatus(); resetSectionViews(); S.arch = null;
  document.getElementById("app").classList.remove("on");
  document.getElementById("picker").style.display = "";
  loadPicker();
}
window.addEventListener("pagehide", () => {
  if (S.arch) navigator.sendBeacon("/api/archive/close", JSON.stringify({ root_id: S.arch.id }));
});
export function renderNav() {
  const el = document.getElementById("navitems"); el.innerHTML = "";
  SECTIONS.forEach(s => {
    const d = document.createElement("button"); d.type = "button";
    d.className = "navitem" + (s.id === S.section ? " active" : "");
    d.title = s.label; d.setAttribute("aria-current", s.id === S.section ? "page" : "false");
    d.innerHTML = `<span class="navicon" aria-hidden="true">${ICONS[s.id]}</span><span>${s.label}</span>`; d.onclick = () => showSection(s.id);
    el.appendChild(d);
  });
  syncThemeControl();
}
function navCollapsed() { return localStorage.getItem("navCollapsed") === "1"; }
function applyNavCollapsed() {
  document.getElementById("nav").classList.toggle("collapsed", navCollapsed());
}
function toggleNav() {
  localStorage.setItem("navCollapsed", navCollapsed() ? "0" : "1");
  applyNavCollapsed();
}
applyNavCollapsed();
const RENDERERS = {
  overview: renderOverview, library: renderPhotos, timeline: renderTimeline, places: renderMap,
  people: renderFaces, pets: renderPets, dups: renderDedup
};
const SECTION_VIEWS = new Map();
const SECTION_READY = new Set();
export let ACTIVE_SECTION = null;
export function resetSectionViews() {
  const main = document.getElementById("main");
  disposeMap();
  if (S.grid && S.grid.observer) S.grid.observer.disconnect();
  S.grid = null; S.gallery = [];
  INFINITE_LIST_KEYS.forEach(key => {
    if (S[key] && S[key].observer) S[key].observer.disconnect();
    S[key] = null;
  });
  SECTION_VIEWS.clear(); SECTION_READY.clear(); ACTIVE_SECTION = null;
  if (main) main.replaceChildren();
}
export function libraryVisibleAnchor() {
  const main = document.getElementById("main"), grid = document.getElementById("grid");
  if (!main || !grid) return null;
  const top = main.getBoundingClientRect().top;
  const tiles = [...grid.querySelectorAll(".tile[data-result-index]")];
  const visible = tiles.find(node => node.getBoundingClientRect().bottom > top);
  return visible ? {
    id: Number(visible.dataset.fileId),
    index: Number(visible.dataset.resultIndex),
    top: visible.getBoundingClientRect().top - top,
  } : null;
}
export function restoreLibraryAnchor(anchor) {
  if (!anchor) return false;
  const main = document.getElementById("main");
  const tile = document.querySelector(`#grid .tile[data-file-id="${anchor.id}"]`);
  if (!main || !tile) return false;
  const top = main.getBoundingClientRect().top;
  main.scrollTop += tile.getBoundingClientRect().top - top - anchor.top;
  return true;
}
function stashActiveSection() {
  if (!ACTIVE_SECTION) return;
  const main = document.getElementById("main"), section = ACTIVE_SECTION;
  if (!SECTION_READY.has(section)) {
    main.replaceChildren(); ACTIVE_SECTION = null; return;
  }
  // The Library can contain hundreds of decoded thumbnails. Preserve its
  // lightweight query/page state and visible anchor, but release every DOM and
  // image node instead of retaining the section as a detached fragment.
  if (section === "library") {
    const g = S.grid;
    if (g) {
      g.anchor = libraryVisibleAnchor();
      g.savedScrollTop = main.scrollTop;
      if (g.observer) g.observer.disconnect();
      g.observer = null;
    }
    main.replaceChildren();
    SECTION_READY.delete(section);
    ACTIVE_SECTION = null;
    return;
  }
  const fragment = document.createDocumentFragment();
  const scrollTop = main.scrollTop;
  while (main.firstChild) fragment.appendChild(main.firstChild);
  SECTION_VIEWS.set(section, { fragment, scrollTop });
  ACTIVE_SECTION = null;
}
function resumeSection(id) {
  if (id === "overview") startPoll();
  else if (id === "library") renderSearchReach();
  else if (id === "people" && document.getElementById("facejob")) startFacePoll();
  else if (id === "pets" && document.getElementById("petjob")) startPetPoll();
  else if (id === "places" && MAP) setTimeout(() => { MAP.invalidateSize(); drawMap(); }, 0);
}
export function showSection(id, reload = false) {
  if (!RENDERERS[id]) id = "overview";
  if (ACTIVE_SECTION === id && !reload) return;
  S.nav++; const gen = S.nav;
  stopPoll();
  const m = document.getElementById("main");
  if (ACTIVE_SECTION) {
    if (reload && ACTIVE_SECTION === id) {
      m.replaceChildren();
      SECTION_VIEWS.delete(id);
      SECTION_READY.delete(id);
      ACTIVE_SECTION = null;
    } else stashActiveSection();
  }
  S.section = id; ACTIVE_SECTION = id; renderNav();
  if (S.arch) location.hash = `/archive/${S.arch.id}/${id}`;
  const saved = SECTION_VIEWS.get(id);
  if (saved && !reload) {
    SECTION_VIEWS.delete(id);
    m.appendChild(saved.fragment);
    requestAnimationFrame(() => { if (ACTIVE_SECTION === id) m.scrollTop = saved.scrollTop; });
    resumeSection(id);
    return;
  }
  SECTION_READY.delete(id);
  m.scrollTop = 0;
  m.innerHTML = '<div class="muted" style="padding:30px">Loading…</div>';
  const fn = RENDERERS[id] || (mm => renderSoon(mm, id));
  // Isolate each section render: a throw (bad fetch, JSON error, …) shows an inline
  // error with Retry instead of leaving the previous section's DOM half-replaced.
  Promise.resolve().then(() => fn(m)).then(() => {
    if (gen === S.nav && ACTIVE_SECTION === id) SECTION_READY.add(id);
  }).catch(err => {
    if (gen !== S.nav) return;
    console.error("section render failed:", id, err);
    m.innerHTML = `<div class="soonbox"><div class="big">⚠️</div>
      <p>Couldn't load this section.</p>
      <p class="muted">${(err && err.message) || err}</p>
      <p style="margin-top:14px"><button class="btn sec" onclick="showSection('${id}',true)">Retry</button></p></div>`;
    SECTION_READY.add(id);
  });
}

/* ---------- overview + tasks ---------- */

/* ---------- date sources (complementary bar shown under the Timeline) ---------- */

/* ---------- timeline ---------- */

function syncMapTiles() {
  syncPlacesMapTiles();
  syncPickerMapTiles();
}

/* ---------- browse ---------- */

/* ---------- source folders + archive maintenance ---------- */
async function renderFolders(m) {
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Folders</h2><p>See where the original files in this archive live.</p></div></div><div class="panel"><div class="muted">Reading folders…</div></div>`;
  const res = await jget("/api/folders?root=" + S.arch.id); const folders = res.folders || [];
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Folders</h2><p>See where the original files in this archive live. Nothing here changes them.</p></div></div><div class="panel"><div class="folder-list">${folders.length ? folders.map(f => `<div class="taskcard"><span class="ico">□</span><div class="body"><div class="t">${esc(f.path)}</div></div><div class="num">${f.count.toLocaleString()}</div></div>`).join("") : '<div class="muted">No catalogued files yet.</div>'}</div></div>`;
}
/* ---------- duplicates ---------- */

/* ---------- faces / people ---------- */

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

function renderSoon(m, id) {
  const S2 = {
    places: ["📍", "Places", "Reverse-geocode coordinates into place names and browse by location."],
    situations: ["🔎", "Situations", "Search by content: “beach sunset”, “birthday”, using local image embeddings."]
  }[id];
  m.innerHTML = `<h2 class="sec">${S2[1]}</h2><div class="soonbox"><div class="big">${S2[0]}</div>
    <p>${S2[2]}</p><p class="muted">Coming in a later phase.</p></div>`;
}

/* ---------- pets / non-human review ---------- */

/* ---------- detail modal (editable: faces / place / date) ---------- */

/* ----- faces: reassign to a named person (pinned server-side) ----- */

/* ----- date: variable precision (year / year-month / year-month-day) ----- */

/* ----- place: attach to a named place, or create one by pin ----- */

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("settings-drawer").classList.contains("open")) {
    closeSettings(); return;
  }
  if (e.key === "Escape") { closeModal(); return; }
  if (!MITEM || !document.getElementById("modal").classList.contains("open")) return;
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const ids = S.gallery || [], at = ids.indexOf(MITEM.id), next = ids[at + (e.key === "ArrowLeft" ? -1 : 1)];
  if (next != null) { e.preventDefault(); openItem(next); }
});

syncThemeControl();
loadPicker().then(applyHash);

// Inline `on*` attributes in the markup -- and in the template literals above that
// generate markup -- are evaluated by the browser against `window`, not against this
// module's scope. Every function named by one of them must therefore be re-exported
// here or its button silently does nothing when clicked, with no error at load time.
// This list is the frontend's public surface; keep it alphabetical.
// `tools/dev/check_handlers.py` fails the build if the two ever disagree.
Object.assign(window, {
  addArchiveFromForm, addPersonPicker, addPetPicker, answerSuggest, applyFilters, applySort,
  applyTimelineFilters, backToPeople, clearFilters, clearTimelineFilters, closeModal,
  closePick, closePlaceCluster, closeSettings, editClusterName, editDate, editPersonName,
  editPlace, hidePerson, mergeAskCancel, newPlace, onAddPerson, onAddPet,
  onPeopleFilterChange, onPlaceSelect, onSemanticComposerInput, onSemanticComposerKeydown,
  onSemanticComposerPaste, onTimelineYearChange, onYearChange, openItem, openSettings,
  reassignFace, removeManualPerson, removeManualPet, renamePet, renderInfo, saveDate,
  saveNewPlace, semanticSubmit, setMapView, setStorageMetric, showSection, toPicker,
  toggleNav, togglePipelinePause, toggleStagePause, toggleTheme, undoMerge,
});
