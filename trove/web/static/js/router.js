// Hash routing and the section shell: which screen is on, the nav that switches
// between them, and the stash/resume that keeps a section's DOM and scroll
// position alive while the user is elsewhere. The RENDERERS table is the one
// place that names every screen's entry point.

import {
  renderUnknownSection,
} from "./cards.js";
import {
  syncThemeControl,
} from "./settings.js";
import {
  jpost,
} from "./api.js";
import {
  docsHashSlug, openDocs,
} from "./docs.js";
import {
  renderDedup, resumeDedup,
} from "./dups.js";
import {
  INFINITE_LIST_KEYS,
} from "./infinite.js";
import {
  renderPhotos,
} from "./library.js";
import {
  renderOverview,
} from "./overview.js";
import {
  renderFaces, startFacePoll, stopFacePoll,
} from "./people.js";
import {
  renderPets, startPetPoll, stopPetPoll,
} from "./pets.js";
import {
  ARCHIVES, loadPicker, openArchive,
} from "./picker.js";
import {
  MAP, disposeMap, drawMap, renderMap,
} from "./places.js";
import {
  renderSearchWays,
} from "./search.js";
import {
  ICONS, S, archiveSections,
} from "./state.js";
import {
  stopPipelinePoll,
} from "./pipeline.js";
import {
  endSelecting,
} from "./select.js";
import {
  renderTimeline, resumeTimeline,
} from "./timeline.js";

export function applyHash() {
  // Reference pages first: they are a top-level screen of their own and answer
  // without an archive, so a bookmark straight into one has to work before any
  // archive has been opened.
  const doc = docsHashSlug(location.hash);
  if (doc) { openDocs(doc); return true; }
  const m = (location.hash || "").match(/#\/archive\/(\d+)\/(\w+)/);
  if (m) { const a = ARCHIVES.find(x => x.id == +m[1]); if (a) { openArchive(a, m[2]); return true; } }
  return false;
}
export function toPicker() {
  if (S.arch) jpost("/api/archive/close", { root_id: S.arch.id });
  stopSectionPolls(); stopPipelinePoll(); resetSectionViews(); S.arch = null;
  document.getElementById("app").classList.remove("on");
  document.getElementById("picker").style.display = "";
  loadPicker();
}
export function renderNav() {
  const el = document.getElementById("navitems"); el.innerHTML = "";
  archiveSections(S.arch).forEach(s => {
    const d = document.createElement("button"); d.type = "button";
    d.className = "navitem" + (s.id === S.section ? " active" : "");
    d.title = s.label; d.setAttribute("aria-current", s.id === S.section ? "page" : "false");
    d.innerHTML = `<span class="navicon" aria-hidden="true">${ICONS[s.id]}</span><span>${s.label}</span>`; d.onclick = () => showSection(s.id);
    el.appendChild(d);
  });
  syncThemeControl();
}
function navCollapsed() { return localStorage.getItem("navCollapsed") === "1"; }
export function applyNavCollapsed() {
  document.getElementById("nav").classList.toggle("collapsed", navCollapsed());
}
export function toggleNav() {
  localStorage.setItem("navCollapsed", navCollapsed() ? "0" : "1");
  applyNavCollapsed();
}
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
  // Browse's view of its own results goes with the grids it described.
  S.onlyWay = ""; S.overviewScrollTop = 0;
  INFINITE_LIST_KEYS.forEach(key => {
    if (S[key] && S[key].observer) S[key].observer.disconnect();
    S[key] = null;
  });
  SECTION_VIEWS.clear(); SECTION_READY.clear(); ACTIVE_SECTION = null;
  if (main) main.replaceChildren();
}
/* The way back, wherever one is offered.

   Three screens drew this and drew it differently: the sidebar and a person's
   page with a stroked chevron, a pet's page and the search results with a
   typed "←" -- which is a different mark at a different weight that does not
   take the icon sizing the rest of the app's controls share. One function so
   there is one arrow, and `label` is what it goes back TO, because a control
   that says where it leads is worth more than one that says it leaves.

   `destination` is not drawn. It is the title and the accessible name, which is
   where "where does this go" belongs: the screen already answers it, since the
   thing you are looking at is the thing you would be leaving. It also settles
   the one label that had nowhere to go -- inside a search ranking this returns
   to the overview of every way, and every phrase for that is a word away from
   "All results", which is on screen a line above meaning something else
   entirely (how far down one ranking to go).

   The nav's own is the fourth, and stays in index.html: it is static markup in
   the shell rather than something a screen builds. */
export function backControl(destination) {
  return `<button class="back back-control" type="button"
      aria-label="Back to ${destination}" title="Back to ${destination}">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
    </button>`;
}
/* Wire the back control inside `host` to `onBack`.

   A listener rather than an inline attribute, which is what lets the markup
   above be shared at all: tools/dev/check_handlers.py reads inline handlers as
   TEXT, so a helper interpolating one would hide three functions from the check
   that proves every inline handler resolves -- and would keep three functions
   on `window` that nothing outside their own screen calls. */
export function onBackControl(host, onBack) {
  const button = host.querySelector(".back-control");
  if (button) button.addEventListener("click", onBack);
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
// The pipeline snapshot is not among these: one poller (pipeline.js) serves
// every screen for as long as an archive is open, so a section never starts or
// stops it. What is left here is the polling a screen genuinely owns -- its own
// summary endpoint -- which only makes sense while that screen is on show.
function stopSectionPolls() { stopFacePoll(); stopPetPoll(); }
// A selection belongs to the grid it was made on. Leaving the screen ends it,
// rather than leaving a bar over the next one offering to merge groups that are
// no longer in front of anybody.
function stopSelecting() { endSelecting(); }
function resumeSection(id) {
  if (id === "library") renderSearchWays();
  else if (id === "people" && document.getElementById("facejob")) startFacePoll();
  else if (id === "pets" && document.getElementById("petjob")) startPetPoll();
  else if (id === "places" && MAP) setTimeout(() => { MAP.invalidateSize(); drawMap(); }, 0);
  // The one filter bar built from data that changes while you are away: naming
  // someone in People has to reach the list of people to narrow by here.
  else if (id === "timeline") resumeTimeline();
  // Duplicates has no poll of its own: it rides the pipeline snapshot, which
  // only reaches it while it is the section on show. So what a stashed screen
  // missed while the user was elsewhere is asked for once, here, on the way
  // back -- otherwise the replayed fragment is exactly as stale as it was left.
  else if (id === "dups") resumeDedup();
}
export function showSection(id, reload = false) {
  // A hash can name a section this archive does not run — a bookmark from
  // before People was switched off, or a link between archives. Fall back to
  // the Overview rather than rendering a screen whose data will never arrive.
  if (!RENDERERS[id] || !archiveSections(S.arch).some(s => s.id === id)) id = "overview";
  // Written before the early return, not after it. A hash naming a section that
  // does not exist falls back to the Overview -- and when the Overview was
  // already the section on show, returning here left the address bar still
  // claiming the section nobody is looking at, so a reload or a copied link
  // reproduced the wrong screen. Assigning the same hash it already holds is a
  // no-op; assigning a different one re-enters here and returns at this line.
  if (S.arch) location.hash = `/archive/${S.arch.id}/${id}`;
  if (ACTIVE_SECTION === id && !reload) return;
  S.nav++; const gen = S.nav;
  stopSectionPolls();
  stopSelecting();
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
  const fn = RENDERERS[id] || (mm => renderUnknownSection(mm, id));
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
