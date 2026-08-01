// The Trove frontend, entry point.
//
// Loaded as `<script type="module">`, so everything in here is module-scoped:
// nothing lands on `window` unless the export block at the bottom puts it there.
// That block is the one thing to read before changing anything above it.

import {
  esc, fmtBytes, fmtDate, toast,
} from "./dom.js";
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
  MAP, MAP_WORLD_BOUNDS, closePlaceCluster, configureMapViewport, disposeMap, drawMap,
  editClusterName, renderMap, replaceMapTiles, setMapView, syncPlacesMapTiles,
  themedTileLayer,
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
  ICONS, S, SECTIONS, TYPE_COL, TYPE_ICON, typeLabel,
} from "./state.js";
import {
  jget, jpost, qpost,
} from "./api.js";

let LOCAL_TRANSLATOR_PROMISE = null, SEARCH_SUBMISSION = 0;

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

function clearlyEnglishSearch(text) {
  // Short-query language detection is unreliable, but these structural words
  // are strong English signals and prevent feeding an already-English phrase
  // such as "besides a lake" through the Spanish translator. Ambiguous words
  // shared with Spanish ("a", "no", "me") are deliberately excluded.
  const signals = new Set(["the", "this", "that", "these", "those", "is", "are", "was", "were",
    "with", "without", "beside", "besides", "near", "by", "at", "of", "and", "or", "from", "to",
    "in", "on", "under", "over", "between", "inside", "outside", "during"]);
  return normalizedWords(text).split(" ").some(word => signals.has(word));
}
// Translate a Spanish query to English before embedding it. SigLIP 2's text
// tower is genuinely multilingual, so this looks redundant — it is not, and
// measurably so. A Spanish query gets hijacked by Spanish text *rendered
// inside* images, which this archive is full of (WhatsApp screenshots, memes,
// posters), because the model reads them. Measured over 30 query pairs on
// 2,000 real files: a Spanish query's top 10 is 57% screenshots against a
// 34% baseline, the English translation's is 30%. "un perro" returns ten dog
// memes; "a dog" returns ten photographs of dogs.
//
// And the wrong results score HIGHER (Spanish beats English on 22 of 30
// queries), which is why the translation must *replace* the original rather
// than be merged with it as an alternate vector — taking the best of both
// would systematically pick the worse one.
async function localEnglishTranslation(text) {
  if (!text || !text.match(/\p{L}/u) || clearlyEnglishSearch(text)) return "";
  try {
    if (!LOCAL_TRANSLATOR_PROMISE) {
      LOCAL_TRANSLATOR_PROMISE = import("/vendor/bergamot-translator.js").then(module =>
        new module.LatencyOptimisedTranslator({
          pivotLanguage: null,
          registryUrl: "/vendor/translation-es-en.json",
          cacheSize: 256,
          downloadTimeout: 15000
        })
      );
    }
    const translator = await LOCAL_TRANSLATOR_PROMISE;
    const response = await translator.translate({ from: "es", to: "en", text, html: false });
    const translated = (response && response.target && response.target.text || "")
      .replace(/\s+/g, " ").trim().toLocaleLowerCase();
    return normalizedWords(translated) === normalizedWords(text) ? "" : translated;
  } catch (error) {
    // Translation improves recall but is never required for search. Reset a
    // failed loader so a transient worker/model error can recover next time.
    console.warn("Local Spanish search expansion unavailable:", error);
    LOCAL_TRANSLATOR_PROMISE = null;
    return "";
  }
}
function visualSearchExpansion(translation) {
  if (!translation) return "";
  const words = new Set(normalizedWords(translation).split(" "));
  // The model is matching text to image vectors. A lightweight photographic
  // cue helps terse translated locations ("in the lake") align with actual
  // photos without changing the translation shown to the user.
  return ["photo", "photos", "picture", "pictures", "image", "images"].some(word => words.has(word))
    ? translation : `${translation} photo`;
}

/* ---------- picker ---------- */
let ARCHIVES = [];
// Build one archive card's cover mosaic from a few real thumbnails (served by
// the root-scoped /archivethumb route, since no archive is "open" here). Cells
// cycle the available thumbs so partial covers still fill the grid; an archive
// with none yet (freshly added, still scanning) shows a calm folder glyph.
function pickerCover(a) {
  const ids = (a.covers || []).filter(x => x != null);
  const badge = a.size ? `<span class="p-badge">${fmtBytes(a.size)}</span>` : "";
  if (!ids.length) {
    return `<div class="p-cover empty"><svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>${badge}</div>`;
  }
  let cells = "";
  for (let i = 0; i < 5; i++) {
    const id = ids[i % ids.length];
    cells += `<div class="cell"><img src="/archivethumb/${a.id}/${id}" loading="lazy" alt="" onerror="this.remove()"></div>`;
  }
  return `<div class="p-cover">${cells}${badge}</div>`;
}
async function loadPicker() {
  const { archives } = await jget("/api/archives"); ARCHIVES = archives;
  const el = document.getElementById("archcards"); el.innerHTML = "";
  const title = document.getElementById("archive-list-title");
  const sum = document.getElementById("arch-summary");
  const totalFiles = archives.reduce((s, a) => s + (a.files || 0), 0);
  title.style.display = archives.length ? "flex" : "none";
  if (sum) sum.textContent = archives.length
    ? `${archives.length} folder${archives.length === 1 ? "" : "s"} · ${totalFiles.toLocaleString()} files`
    : "";
  archives.forEach(a => {
    const c = document.createElement("div");
    c.className = "p-card"; c.setAttribute("role", "button"); c.tabIndex = 0;
    c.onclick = () => openArchive(a);
    c.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openArchive(a); } };
    const warn = a.exists ? "" : ` · <span class="warn">not mounted</span>`;
    c.innerHTML = pickerCover(a) +
      `<div class="p-meta">
             <button class="p-remove" type="button" aria-label="Remove archive">Remove</button>
             <div class="nm">${esc(a.name)}</div>
             <div class="st">${a.files.toLocaleString()} files${warn}</div>
           </div>`;
    c.querySelector(".p-remove").onclick = (event) => { event.stopPropagation(); removeArchive(a); };
    el.appendChild(c);
  });
  const add = document.createElement("button");
  add.className = "p-card add"; add.type = "button";
  add.innerHTML = `<span>+</span>${archives.length ? "Add another folder" : "Add your first folder"}`;
  add.onclick = () => startAddArchive();
  el.appendChild(add);
}
async function startAddArchive() {
  const field = document.getElementById("archive-path"); let p = field.value.trim();
  if (window.archiveDesktop?.chooseFolder) {
    const picked = await window.archiveDesktop.chooseFolder();
    if (picked.cancelled) return false;
    p = picked.path || ""; field.value = p;
  }
  if (!p) { highlightAddArchiveField(); return false; }
  const r = await jpost("/api/archives", { path: p });
  // Re-read the list either way. A rejected add used to return early, so if
  // the server had already recorded anything the start page kept showing the
  // stale set until a full page reload made it look like the folder appeared
  // out of nowhere.
  field.value = ""; await loadPicker();
  if (r.error) { toast(r.error, true); return false; }
  const a = ARCHIVES.find(x => x.id === r.id) || { id: r.id, path: r.path, name: r.path.split("/").filter(Boolean).pop(), files: 0, size: 0, exists: true };
  openArchive(a, "overview"); return false;
}
function highlightAddArchiveField() {
  const field = document.getElementById("archive-path"); const wrap = field.closest(".p-add");
  field.focus(); field.scrollIntoView({ block: "center" });
  if (!wrap) return;
  wrap.classList.add("needs-path");
  clearTimeout(wrap._needsPathTimer);
  wrap._needsPathTimer = setTimeout(() => wrap.classList.remove("needs-path"), 1500);
  field.addEventListener("input", () => wrap.classList.remove("needs-path"), { once: true });
}
async function addArchiveFromForm(event) {
  event.preventDefault();
  return startAddArchive();
}
async function removeArchive(a) {
  const message = `Remove “${a.name}” from Trove?\n\nThis removes its catalog entries and exclusive cached thumbnails. Your original files in:\n${a.path}\nwill not be changed.`;
  if (!confirm(message)) return;
  const r = await jpost("/api/archive/remove", { root_id: a.id });
  if (r.error) { alert(r.error); return; }
  ARCHIVES = ARCHIVES.filter(x => x.id !== a.id);
  await loadPicker();
}

/* ---------- archive shell ---------- */
function openArchive(a, section) {
  if (S.arch && S.arch.id !== a.id) jpost("/api/archive/close", { root_id: S.arch.id });
  resetSectionViews();
  // Don't carry a previous archive's idle status into this one. Until its disk
  // check finishes, we genuinely do not know whether work is waiting.
  S.arch = a; S.section = section || "overview";
  // One source of truth for status now: the /api/pipeline snapshot. Don't
  // carry a previous archive's status into this one.
  S.pipeline = null; S.pipeActive = false;
  document.getElementById("picker").style.display = "none";
  document.getElementById("app").classList.add("on");
  document.getElementById("archname").textContent = a.name;
  location.hash = `/archive/${a.id}/${S.section}`;
  jpost("/api/archive/open", { root_id: a.id });
  showSection(S.section); startGlobalStatus();
}

/* ---------- persistent pipeline status (sidebar, shown on every section) ----
   The pipeline runs itself; this ambient chip is the only status the user
   needs and it carries no controls. */
const JOB_LABEL = {
  scan: "Scanning files…", enrich: "Reading metadata…", detect: "Detecting people & pets…",
  face_cluster: "Updating people…", places: "Updating map places…", dedup: "Finding duplicates…", semantic: "Indexing search…"
};
const JOB_DESCRIPTION = {
  scan: "Checking the archive folder and cataloging new or changed files.",
  enrich: "Reading file details to find dates, locations, and other metadata.",
  detect: "Finding people and animals in each photo (one pass) and grouping them into people and pets.",
  face_cluster: "Reclustering People after a non-human review correction.",
  places: "Grouping geotagged files into map places while keeping your edits.",
  dedup: "Comparing file content to identify duplicate copies.",
  semantic: "Creating search entries so media can be found by a description."
};
function jobDescription(kind) { return JOB_DESCRIPTION[kind] || "Updating archive data."; }
// The sidebar chip and the Overview health cards read the SAME pipeline
// snapshot, so they can never tell the user two different things.
export const CARD_KIND = { scan: "scan", dedup: "dedup", detect: "detect", places: "places", semantic: "semantic" };
// Percentages arrive with one decimal; whole numbers read calmer in an
// ambient chip, and "<1%" beats a "0%" that looks stalled.
function gstatPct(pct) {
  if (pct > 0 && pct < 1) return "&lt;1%";
  return Math.min(100, Math.round(pct)) + "%";
}
function gstatRow(run) {
  const pct = run.progress && run.progress.percent != null ? run.progress.percent : null;
  // The label's trailing ellipsis meant "in progress"; the bar says that now,
  // and dropping it keeps the real text-overflow ellipsis unambiguous.
  const label = (JOB_LABEL[CARD_KIND[run.id]] || run.label).replace(/…$/, "");
  return `<div class="grow"><div class="gline"><span class="gtxt">${label}</span>`
    + (pct != null ? `<span class="gpct">${gstatPct(pct)}</span>` : "") + `</div>`
    + (pct != null
      ? `<div class="gbar"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`
      : `<div class="gbar ind"><i></i></div>`)
    + `</div>`;
}
export function renderGstat(snap) {
  const el = document.getElementById("gstat"); if (!el) return;
  // Stages plus the non-stage jobs a user action kicks (face_cluster /
  // pet_cluster). Those hold the writer lock too, so leaving them out made
  // the app look stalled for no visible reason.
  const runs = snap && snap.stages
    ? snap.stages.filter(s => s.state === "running").concat(snap.extra || [])
    : [];
  // Several PARALLEL_KINDS stages can run at once; one row each. The
  // collapsed rail falls back to .gmini (see the .gstat CSS).
  const mini = `<span class="gmini"><span class="spin"></span>`
    + (runs.length > 1 ? `<span class="gcount">×${runs.length}</span>` : "") + `</span>`;
  if (runs.length) {
    el.title = runs.map(r => `${r.label}: ${r.message || ""}`).join("\n");
    el.innerHTML = mini + runs.map(gstatRow).join("");
  } else if (!snap) {
    el.title = "Checking for new work…";
    el.innerHTML = `<div class="gstate"><span class="dot pending"></span><span class="gtxt">Checking for new work…</span></div>`;
  } else if (snap.overall === "idle") {
    el.title = "Up to date";
    el.innerHTML = `<div class="gstate"><span class="dot ok"></span><span class="gtxt">Up to date</span></div>`;
  } else if (snap.overall === "paused") {
    // Nothing is actually running (the `runs` branch above already
    // caught that); reads as stopped, not "Working…".
    el.title = "Background processing is paused";
    el.innerHTML = `<div class="gstate"><span class="dot check"></span><span class="gtxt">Paused</span></div>`;
  } else {
    // Work is waiting to run (queued/blocked) but nothing is on the writer yet.
    el.title = "Work is queued";
    el.innerHTML = `<div class="gstate"><span class="dot pending"></span><span class="gtxt">Working…</span></div>`;
  }
}
async function gstatTick() {
  if (!S.arch) { stopGlobalStatus(); return; }
  try {
    const snap = await jget("/api/pipeline?root=" + S.arch.id);
    S.pipeline = snap;
    renderGstat(snap);
    // A library opened just before the scanner commits its first batch used to
    // remain an empty wall until the user manually changed a filter or route.
    // Refresh only that empty state, so active browsing is never interrupted.
    const scanning = (snap.stages || []).some(s => s.id === "scan" && s.state === "running"), g = S.grid;
    if (scanning && S.section === "library" && g && g.loaded === 0 && !g.refreshing) {
      g.refreshing = true;
      setTimeout(() => {
        if (S.section === "library" && S.grid === g && g.loaded === 0) {
          resetGridResults(g);
          loadGrid().finally(() => { if (S.grid === g) g.refreshing = false; });
        } else if (S.grid === g) {
          g.refreshing = false;
        }
      }, 1500);
    }
  } catch {
    // A poll tick that fails is a non-event: the next one is two seconds away
    // and the chip simply keeps its last value. Reporting it would fill the
    // console every time the server restarts under the user.
  }
}
function startGlobalStatus() { stopGlobalStatus(); S.gpoll = setInterval(gstatTick, 2000); gstatTick(); }
function stopGlobalStatus() { if (S.gpoll) { clearInterval(S.gpoll); S.gpoll = null; } }
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
function resetSectionViews() {
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
function libraryVisibleAnchor() {
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
function restoreLibraryAnchor(anchor) {
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
export const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
const GRID_PAGE_SIZE = 120, GRID_MAX_PAGES = 4;
async function renderPhotos(m) {
  const gen = S.nav;
  const restored = !!(S.grid && Array.isArray(S.grid.pages));
  const g = restored ? S.grid : {
    offset: 0, loaded: 0, gen: 0, year: "", month: "", type: "", people: [], inferredPeople: [],
    place: "", onlyIndexed: false, onlyLocated: false, rawQuery: "", searchedQuery: "", query: "", expandedQuery: "",
    expandedEmbeddingQuery: "", sort: "",
    total: null, doneDown: false, doneUp: true, loadingGen: null, observer: null, pages: [],
    anchor: null, savedScrollTop: 0,
  };
  S.grid = g;
  S.gallery = g.pages.flatMap(page => page.items.map(item => item.id));
  m.innerHTML = `<div class="pagehead">
      <div><h2 class="sec">Library</h2>
      <p>Browse and search every item, with filters that work together.</p></div>
    </div>
    <div class="library-controls">
      <form class="library-search" onsubmit="return semanticSubmit(event)">
        <div class="semantic-composer" id="semantic-q" contenteditable="true" role="textbox"
          aria-label="Search your library by description" data-placeholder="Search your library, describe anything"
          spellcheck="true" oninput="onSemanticComposerInput()" onkeydown="onSemanticComposerKeydown(event)"
          onpaste="onSemanticComposerPaste(event)"></div>
        <button class="btn" type="submit">Search</button>
      </form>
      <div class="active-query" id="active-query" aria-live="polite" hidden></div>
      <div class="search-reach" id="search-reach" aria-live="polite" hidden></div>
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
  const composer = document.getElementById("semantic-q");
  composer.addEventListener("compositionstart", () => S.composerComposing = true);
  composer.addEventListener("compositionend", () => { S.composerComposing = false; onSemanticComposerInput(); });
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

/* ---------- generic forward-only infinite scroll ----------
   Every catalog list outside the Library (Duplicates, People, Pets, and
   their detail grids) is always entered at offset 0 and only grows
   downward -- unlike the Library grid's filtered/date-jump entry points,
   so a single bottom sentinel is enough; no prepend, no anchor restore.
   Mirrors loadGrid()'s auto-refill-while-visible behavior above.
   `stateKey` names an S.<key> slot: starting a new list for the same key
   disconnects the previous observer and makes any of its still-in-flight
   fetch a no-op once it lands (the same staleness guard the Library grid
   gets from `S.grid !== g`), so a merge/reload can't paint stale cards
   into a grid that has since been reset. */
export function startInfiniteList(stateKey, { sentinelId, pageSize, fetchPage, onPage, root }) {
  if (S[stateKey] && S[stateKey].observer) S[stateKey].observer.disconnect();
  const scrollRoot = root || document.getElementById("main");
  const sentinelEl = () => document.getElementById(sentinelId);
  function isNear() {
    const s = sentinelEl();
    if (!s || !scrollRoot || !s.isConnected) return false;
    const sr = s.getBoundingClientRect(), rr = scrollRoot.getBoundingClientRect();
    return sr.top <= rr.bottom + 600;
  }
  const state = { offset: 0, done: false, loading: false, observer: null };
  async function loadMore() {
    if (S[stateKey] !== state || state.done || state.loading) return;
    const first = state.offset === 0;
    state.loading = true;
    let s = sentinelEl(); if (s) s.innerHTML = '<span class="spin"></span>Loading…';
    let failed = false;
    try {
      const items = await fetchPage(state.offset, pageSize);
      if (S[stateKey] !== state) return;
      state.offset += items.length;
      state.done = items.length < pageSize;
      onPage(items, { first, done: state.done });
      s = sentinelEl(); if (s) s.textContent = "";
    } catch (error) {
      failed = true;
      s = sentinelEl();
      if (S[stateKey] === state && s) s.textContent = "Couldn’t load more. Scroll away and back to retry.";
    } finally {
      state.loading = false;
      requestAnimationFrame(() => {
        if (!failed && S[stateKey] === state && !state.done && isNear()) loadMore();
      });
    }
  }
  const sentinel = sentinelEl();
  if (sentinel) {
    state.observer = new IntersectionObserver(entries => {
      if (S[stateKey] !== state || state.done) return;
      if (entries.some(entry => entry.isIntersecting)) loadMore();
    }, { root: scrollRoot, rootMargin: "600px 0px" });
    state.observer.observe(sentinel);
  }
  S[stateKey] = state;
  loadMore();
  return state;
}
// Every S.<key> a startInfiniteList() call site uses; swept on archive
// switch (resetSectionViews) so no orphaned observer keeps a detached
// sentinel from a closed archive alive.
const INFINITE_LIST_KEYS = [
  "dupList", "peopleList", "personDetailList",
  "petListState", "loosePetState", "nonhumanState", "petDetailList", "placeList",
];
// Natural singular/plural label for a media type, so the reach line reads
// "1 video" / "12 videos" rather than a bare type slug.
function reachTypeLabel(type, n) {
  const forms = {
    image: ["image", "images"], video: ["video", "videos"],
    audio: ["audio file", "audio files"], document: ["document", "documents"],
    archive: ["compressed file", "compressed files"], other: ["file", "files"],
  }[type] || [type, type + "s"];
  return forms[n === 1 ? 0 : 1];
}
// Each fresh render supersedes any earlier poll chain (leaving and returning
// to Library must not leave two timers fetching in parallel).
let SEARCH_REACH_GEN = 0;
function renderSearchReach() { searchReachTick(++SEARCH_REACH_GEN); }
async function searchReachTick(gen) {
  if (gen !== SEARCH_REACH_GEN || S.section !== "library" || !S.arch) return;
  const el = document.getElementById("search-reach"); if (!el) return;
  let s;
  try { s = await jget("/api/browse/semantic/status?root=" + S.arch.id); }
  catch {
    const cur = document.getElementById("search-reach");
    if (cur && gen === SEARCH_REACH_GEN) {
      cur.hidden = false;
      cur.innerHTML = `<span class="reach-note">Description search is unavailable right now.</span>`;
    }
    return;
  }
  if (gen !== SEARCH_REACH_GEN || S.section !== "library") return;
  const cur = document.getElementById("search-reach"); if (!cur) return;
  const by = (s.by_type || []).filter(t => t.count > 0);
  const pending = s.pending || 0;
  let html;
  if (by.length) {
    const chips = by.map(t =>
      `<span class="reach-item"><span class="reach-key" style="background:${TYPE_COL[t.type] || TYPE_COL.other}"></span><b>${t.count.toLocaleString()}</b> ${reachTypeLabel(t.type, t.count)}</span>`
    ).join("");
    // Some of the archive is already searchable; if more is still queued, say
    // so in the same breath rather than a separate alarming line.
    const note = pending ? `searchable by description · ${pending.toLocaleString()} more queued for indexing`
      : "searchable by description";
    html = `${chips}<span class="reach-div" aria-hidden="true"></span><span class="reach-note">${note}</span>`;
  } else if (!s.configured) {
    html = `<span class="reach-note">Search by description isn’t available in this installation.</span>`;
  } else if (pending) {
    // Nothing indexed yet, but work is queued: promise it, with no "0 files"
    // chip — a colour-keyed count of zero has nothing to key.
    html = `<span class="reach-note">No files searchable by description yet · ${pending.toLocaleString()} queued for indexing</span>`;
  } else {
    // Nothing indexed and nothing queued (e.g. an empty archive): no promise to make.
    html = `<span class="reach-note">No files searchable by description yet.</span>`;
  }
  cur.hidden = false;
  cur.innerHTML = html;
  // Indexing runs automatically; keep the counts live until it drains.
  if (s.configured && pending) setTimeout(() => searchReachTick(gen), 2500);
}
function normalizedWords(value) {
  return (value || "").normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase()
    .replace(/[’']/g, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}
function editDistance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let diagonal = row[0]; row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const above = row[j], cost = a[i - 1] === b[j - 1] ? 0 : 1;
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, diagonal + cost); diagonal = above;
    }
  }
  return row[b.length];
}
function personWordMatches(queryWord, nameWord) {
  if (queryWord === nameWord) return { matched: true, exact: true };
  // Only the typed word may be a prefix of the name. This lets "mari " match
  // María but prevents a longer unrelated word such as "marinero" from doing so.
  if (queryWord.length >= 4 && nameWord.startsWith(queryWord)) return { matched: true, exact: false };
  if (queryWord.length >= 5 && Math.abs(nameWord.length - queryWord.length) <= 1 &&
    editDistance(nameWord, queryWord) <= 1) return { matched: true, exact: false };
  return { matched: false, exact: false };
}
function extractPeopleMentions(query, people, commitEnd = false) {
  const wordPattern = /[\p{L}\p{M}]+(?:[’'][\p{L}\p{M}]+)*/gu;
  const words = [...query.matchAll(wordPattern)].map(match => {
    const source = match[0], withoutPossessive = source.replace(/[’']s$/iu, "");
    return {
      start: match.index, end: match.index + withoutPossessive.length, source: withoutPossessive,
      norm: normalizedWords(withoutPossessive)
    };
  });
  const candidates = [];
  (people || []).forEach(person => {
    const nameWords = normalizedWords(person.name).split(" ").filter(Boolean);
    if (!nameWords.length) return;
    for (let i = 0; i + nameWords.length <= words.length; i++) {
      let exact = 0, matched = true;
      for (let j = 0; j < nameWords.length; j++) {
        const result = personWordMatches(words[i + j].norm, nameWords[j]);
        if (!result.matched) { matched = false; break; }
        if (result.exact) exact++;
      }
      if (matched) candidates.push({
        person, start: words[i].start, end: words[i + nameWords.length - 1].end,
        source: query.slice(words[i].start, words[i + nameWords.length - 1].end),
        wordCount: nameWords.length, exact
      });
    }
  });
  // Prefer the longest and most exact name at each position. Equally-good
  // ambiguous prefixes are left as text instead of silently choosing a person.
  const mentions = []; let usedUntil = -1;
  [...new Set(candidates.map(candidate => candidate.start))].sort((a, b) => a - b).forEach(start => {
    if (start < usedUntil) return;
    const here = candidates.filter(candidate => candidate.start === start)
      .sort((a, b) => b.wordCount - a.wordCount || b.exact - a.exact);
    if (!here.length) return;
    const best = here[0], tied = here.filter(candidate =>
      candidate.wordCount === best.wordCount && candidate.exact === best.exact);
    if (tied.length > 1) return;
    const next = query.slice(best.end, best.end + 1);
    best.committed = commitEnd ||
      best.end < query.length && !!next.match(/[^\p{L}\p{M}]/u) ||
      best.end === query.length && best.exact === best.wordCount;
    mentions.push(best); usedUntil = best.end;
  });
  return mentions;
}
function peopleMentionedInQuery(query, people) {
  return [...new Set(extractPeopleMentions(query, people, true).map(
    mention => String(mention.person.id)))];
}
function semanticTextWithoutPeople(query, mentions) {
  let cursor = 0, output = "";
  mentions.forEach(mention => { output += query.slice(cursor, mention.start) + " "; cursor = mention.end; });
  output += query.slice(cursor);
  return output.replace(/\s+/g, " ").replace(/^\s*[’']s\b\s*/i, "")
    .replace(/^\s*(?:and|with|y|con)\b\s*/i, "")
    .replace(/\s+([,.;!?])/g, "$1").trim();
}
function semanticComposerText() {
  const composer = document.getElementById("semantic-q");
  return composer ? (composer.textContent || "").replace(/\u00a0/g, " ") : "";
}
function semanticComposerCaret() {
  const composer = document.getElementById("semantic-q"), selection = getSelection();
  if (!composer || !selection.rangeCount || !composer.contains(selection.anchorNode)) return null;
  const range = selection.getRangeAt(0).cloneRange(); range.selectNodeContents(composer);
  range.setEnd(selection.anchorNode, selection.anchorOffset); return range.toString().length;
}
function setSemanticComposerCaret(offset) {
  const composer = document.getElementById("semantic-q"); if (!composer || offset == null) return;
  const range = document.createRange(); let remaining = offset;
  for (const node of composer.childNodes) {
    const length = (node.textContent || "").length;
    if (node.nodeType === Node.TEXT_NODE && remaining <= length) {
      range.setStart(node, remaining); range.collapse(true);
      const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); return;
    }
    if (node.nodeType === Node.ELEMENT_NODE && remaining <= length) {
      // Person tokens are contenteditable=false, so a caret restored inside one
      // leaves the composer focused but unable to accept the next character.
      // Treat the whole token as one atomic item and restore at its edge.
      if (remaining === 0) range.setStartBefore(node);
      else range.setStartAfter(node);
      range.collapse(true);
      const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); return;
    }
    remaining -= length;
  }
  range.selectNodeContents(composer); range.collapse(false);
  const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
}
function renderSemanticComposer(commitEnd = false) {
  const composer = document.getElementById("semantic-q"); if (!composer || S.composerComposing) return;
  const query = semanticComposerText(), caret = semanticComposerCaret();
  const mentions = extractPeopleMentions(query, (S.filterOpts && S.filterOpts.people) || [], commitEnd)
    .filter(mention => mention.committed);
  const fragment = document.createDocumentFragment(); let cursor = 0;
  mentions.forEach(mention => {
    if (mention.start > cursor) fragment.append(document.createTextNode(query.slice(cursor, mention.start)));
    const token = document.createElement("span");
    token.className = "person-token";
    token.dataset.personId = mention.person.id;
    token.contentEditable = "false";
    token.dataset.tooltip = `Filters to media containing ${mention.person.name}`;
    token.tabIndex = 0;
    token.setAttribute("aria-label", `${mention.person.name}, person filter`);
    token.textContent = mention.source;
    fragment.append(token); cursor = mention.end;
  });
  if (cursor < query.length) fragment.append(document.createTextNode(query.slice(cursor)));
  composer.replaceChildren(fragment);
  if (caret != null) {
    composer.focus({ preventScroll: true });
    setSemanticComposerCaret(caret);
  }
}
function onSemanticComposerInput() {
  renderSemanticComposer(false);
  // Keep the composer's text in grid state on every keystroke, not just on
  // submit, so leaving the Library and coming back returns a half-typed
  // search instead of an empty box.
  if (S.grid) S.grid.rawQuery = semanticComposerText();
}
function onSemanticComposerKeydown(event) {
  if (event.key === "Enter") { event.preventDefault(); event.currentTarget.closest("form").requestSubmit(); }
}
function onSemanticComposerPaste(event) {
  event.preventDefault();
  document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
}
function setPeopleChecks(prefix, ids) {
  const chosen = new Set(ids.map(String));
  document.querySelectorAll(`#${prefix}-people-filter input[type="checkbox"]`)
    .forEach(input => input.checked = chosen.has(input.value));
}
async function semanticSubmit(ev) {
  ev.preventDefault();
  const submission = ++SEARCH_SUBMISSION, form = ev.currentTarget;
  const submit = form.querySelector('button[type="submit"]'), oldLabel = submit.textContent;
  const rawQuery = semanticComposerText().trim();
  const g = S.grid;
  const mentions = extractPeopleMentions(rawQuery, (S.filterOpts && S.filterOpts.people) || [], true);
  const mentioned = [...new Set(mentions.map(mention => String(mention.person.id)))];
  const previouslyInferred = new Set((g.inferredPeople || []).map(String));
  const manuallySelected = checkedPeople("f").filter(id => !previouslyInferred.has(String(id)));
  const selected = [...new Set([...manuallySelected, ...mentioned])];
  setPeopleChecks("f", selected);
  g.inferredPeople = mentioned;
  g.people = selected;
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  const menu = document.getElementById("f-people-filter"); if (menu) menu.removeAttribute("open");
  // The visible sentence stays intact, but recognized names are represented by
  // structured filters and removed from the text that gets embedded.
  g.rawQuery = rawQuery;
  g.searchedQuery = rawQuery;
  // Natural-language image retrieval should not depend on how Caps Lock was
  // used. Keep rawQuery intact for the composer, but normalize the text sent
  // through translation and semantic embedding.
  g.query = semanticTextWithoutPeople(rawQuery, mentions).toLocaleLowerCase();
  renderSemanticComposer(true);
  renderSortOptions(g);
  renderActiveQuery(g);
  renderIndexedFilter(g);
  if (submit) { submit.disabled = true; submit.textContent = "Searching…"; }
  const expandedQuery = await localEnglishTranslation(g.query);
  if (submission !== SEARCH_SUBMISSION || S.grid !== g) return false;
  g.expandedQuery = expandedQuery;
  g.expandedEmbeddingQuery = visualSearchExpansion(expandedQuery);
  renderActiveQuery(g);
  if (submit) { submit.disabled = false; submit.textContent = oldLabel; }
  resetGridResults(g);
  updateClearBtn();
  loadGrid();
  return false;
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
  // Indexing coverage. One box, not a select: the only question worth
  // asking is "show me what is done", and the unchecked state already
  // means "everything". Disabled with a reason until anything is indexed,
  // as the places filter waits for a named place.
  parts.push(`<label class="fcheck" id="f-indexed-box">` +
    `<input type="checkbox" id="f-indexed" onchange="applyFilters()">` +
    `Show only indexed files</label>`);
  // The companion to the 📍 tile badge, same as the box above is to the pip:
  // each marker the grid draws should be something you can also filter down
  // to, instead of only being able to spot it by eye.
  parts.push(`<label class="fcheck" id="f-located-box">` +
    `<input type="checkbox" id="f-located" onchange="applyFilters()">` +
    `Show only files with a location</label>`);
  parts.push(`<button class="linkbtn" id="f-clear" onclick="clearFilters()" style="display:none">Clear filters</button>`);
  bar.innerHTML = parts.join("");
  renderIndexedFilter(S.grid);
  renderLocatedFilter();
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
/* A description search only ever returns media it was able to find, so
   every hit is already indexed and this box cannot change the answer.
   Rather than let it sit there appearing to work, it goes disabled for as
   long as a query is running. The checked state is kept, not cleared, so
   going back to plain browsing restores what the user set. */
function renderIndexedFilter(g) {
  const box = document.getElementById("f-indexed"); if (!box) return;
  const searching = !!(g && g.query);
  const live = !!(S.filterOpts && S.filterOpts.indexed_any);
  box.disabled = searching || !live;
  const wrap = document.getElementById("f-indexed-box");
  if (wrap) {
    wrap.classList.toggle("is-disabled", box.disabled);
    wrap.title = searching
      ? "A description search only ever returns indexed files"
      : live ? "Hide files that description search cannot reach yet"
        : "Nothing is indexed yet — indexing has not reached any media";
  }
}
/* Unlike the indexed box this one stays live during a description search:
   narrowing "a dog" down to the geotagged ones is a real question, and
   semantic_search takes the filter. It only goes dead when the archive has
   no located media at all, where it could return nothing but an empty grid. */
function renderLocatedFilter() {
  const box = document.getElementById("f-located"); if (!box) return;
  const live = !!(S.filterOpts && S.filterOpts.located_any);
  box.disabled = !live;
  const wrap = document.getElementById("f-located-box");
  if (wrap) {
    wrap.classList.toggle("is-disabled", !live);
    wrap.title = live
      ? "Show only media that carries a location, marked 📍 in the grid"
      : "No media in this archive has a location";
  }
}
/* The line under the search box states which search the grid below is
   answering. It is not a copy of the box: the box is a draft the user can
   keep editing, and recognized names are shown as the person filters they
   actually became, so what ran is never in doubt. */
function renderActiveQuery(g) {
  const el = document.getElementById("active-query"); if (!el) return;
  const searched = (g.searchedQuery || "").trim();
  el.hidden = !searched;
  if (!searched) { el.replaceChildren(); return; }
  const label = document.createElement("span");
  label.className = "aq-label"; label.textContent = "Results for";
  const phrase = document.createElement("span");
  phrase.className = "aq-phrase";
  const mentions = extractPeopleMentions(
    searched, (S.filterOpts && S.filterOpts.people) || [], true)
    .filter(mention => mention.committed);
  let cursor = 0;
  mentions.forEach(mention => {
    if (mention.start > cursor)
      phrase.append(document.createTextNode(searched.slice(cursor, mention.start)));
    const token = document.createElement("span");
    token.className = "person-token";
    token.dataset.tooltip = `Filtered to media containing ${mention.person.name}`;
    token.tabIndex = 0;
    token.setAttribute("aria-label", `${mention.person.name}, person filter`);
    token.textContent = mention.source;
    phrase.append(token); cursor = mention.end;
  });
  if (cursor < searched.length) phrase.append(document.createTextNode(searched.slice(cursor)));
  // The vector search runs on an English rendering of the sentence. Say so
  // on hover when it differs, so an unexpected match has an explanation.
  if (g.expandedQuery && g.query && g.expandedQuery !== g.query)
    phrase.title = `Searched in English as “${g.expandedQuery}”`;
  const clear = document.createElement("button");
  clear.type = "button"; clear.className = "linkbtn aq-clear";
  clear.textContent = "Clear search";
  clear.onclick = clearSearch;
  el.replaceChildren(label, phrase, clear);
}
function clearSearch() {
  const composer = document.getElementById("semantic-q");
  if (!composer) return;
  composer.replaceChildren();
  composer.closest("form").requestSubmit();
}
/* Sort: date only for now. "" means the list's natural order -- best match
   while a description search is active, newest first when just browsing --
   so the option only needs spelling out as "Newest first" when there is no
   search to rank against. */
function renderSortOptions(g) {
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
function applySort() {
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
  const indexedBox = document.getElementById("f-indexed");
  if (indexedBox) { indexedBox.checked = !!g.onlyIndexed; renderIndexedFilter(g); }
  const locatedBox = document.getElementById("f-located");
  if (locatedBox) { locatedBox.checked = !!g.onlyLocated; renderLocatedFilter(); }
  setPeopleChecks("f", g.people || []);
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  const composer = document.getElementById("semantic-q");
  if (composer) { composer.textContent = g.rawQuery || ""; renderSemanticComposer(true); }
  updateClearBtn();
}
function onYearChange() {
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
function onPeopleFilterChange(prefix) {
  if (prefix === "tl") applyTimelineFilters();
  else { if (S.grid) S.grid.inferredPeople = []; applyFilters(); }
}
function applyFilters() {
  const g = S.grid;
  g.year = selVal("f-year");
  const mm = selVal("f-month");
  g.month = (g.year && mm) ? `${g.year}-${mm}` : "";
  g.type = selVal("f-type");
  g.people = checkedPeople("f");
  g.place = selVal("f-place");
  const indexedBox = document.getElementById("f-indexed");
  g.onlyIndexed = !!(indexedBox && indexedBox.checked);
  const locatedBox = document.getElementById("f-located");
  g.onlyLocated = !!(locatedBox && locatedBox.checked);
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  resetGridResults(g);
  updateClearBtn();
  loadGrid();
}
function resetGridResults(g) {
  g.offset = 0; g.loaded = 0; g.total = null; g.doneDown = false; g.doneUp = true; g.gen++;
  g.pages = []; g.anchor = null; g.savedScrollTop = 0;
  S.gallery = [];
  const grid = document.getElementById("grid"); if (grid) grid.replaceChildren();
  const count = document.getElementById("gridcount"); if (count) count.textContent = "";
  const main = document.getElementById("main"); if (main) main.scrollTop = 0;
}
function clearFilters() {
  ["f-year", "f-type", "f-place"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
  const indexedBox = document.getElementById("f-indexed"); if (indexedBox) indexedBox.checked = false;
  const locatedBox = document.getElementById("f-located"); if (locatedBox) locatedBox.checked = false;
  clearPeopleChecks("f");
  S.grid.inferredPeople = [];
  const msel = document.getElementById("f-month");
  if (msel) { msel.innerHTML = '<option value="">All months</option>'; msel.disabled = true; }
  applyFilters();
}
function updateClearBtn() {
  const g = S.grid, b = document.getElementById("f-clear");
  if (b) b.style.display = (g.year || g.month || g.type || g.people.length || g.place || g.onlyIndexed || g.onlyLocated) ? "inline" : "none";
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
  if (anchor) restoreLibraryAnchor(anchor);
}
async function loadGrid(direction = "append") {
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
    // Not sent to the semantic endpoint: every hit it returns is indexed,
    // so the parameter would be inert there (see renderIndexedFilter).
    if (!g.query && g.onlyIndexed) p.set("indexed", "yes");
    // Sent to both endpoints: a description search can be narrowed by
    // location just as the plain grid can.
    if (g.onlyLocated) p.set("located", "yes");
    if (g.sort) p.set("sort", g.sort);
    if (g.query) {
      // When local translation succeeds it replaces, rather than supplements,
      // the Spanish semantic query. This avoids admitting weak matches unique to
      // the original-language vector. English and fallback searches use g.query.
      p.append("q", g.expandedEmbeddingQuery || g.query);
    }
    const endpoint = g.query ? "/api/browse/semantic/search?" : "/api/media?";
    const res = await jget(endpoint + p);
    // Bail if a newer filter change (or a section switch) superseded this fetch
    // while it was in flight, so a slow response can't paint stale tiles.
    if (S.grid !== g || g.gen !== gen) return;
    const items = (res && res.items) || [];
    const count = (res && res.count) || 0;
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
    g.doneDown = !!(res && res.error) || !windowLast ||
      windowLast.offset + windowLast.items.length >= g.total;
    renderGridPages(g, anchor);
    if (!g.loaded)
      grid.innerHTML = `<div class="muted" style="grid-column:1/-1;padding:40px;text-align:center">${res && res.error ? esc(res.error) : "No media matches these filters."}</div>`;
    const gc = document.getElementById("gridcount");
    if (gc) gc.textContent = gridCountLabel(g);
    const bottom = document.getElementById("grid-sentinel");
    if (bottom) bottom.textContent = g.doneDown ? "" : "Scroll to load more";
  } catch (error) {
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
let MITEM = null;                 // the currently-open item, mutated in place on edit

export async function openItem(id) {
  MITEM = await jget("/api/item/" + id);
  const m = document.getElementById("mmedia");
  if (MITEM.type === "image") m.innerHTML = `<img src="/file/${id}">`;
  else if (MITEM.type === "video") m.innerHTML = `<video src="/file/${id}" controls autoplay></video>`;
  else if (MITEM.type === "audio") m.innerHTML = `<div style="padding:40px"><div class="ph" style="font-size:60px;text-align:center">🎵</div><audio src="/file/${id}" controls autoplay></audio></div>`;
  else m.innerHTML = `<div class="ph" style="font-size:70px;padding:60px">${TYPE_ICON[MITEM.type] || "📦"}</div>`;
  renderInfo();
  document.getElementById("modal").classList.add("open");
}
function renderInfo() {
  closePick();
  const it = MITEM;
  const kv = (k, v) => v != null && v !== "" ? `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>` : "";
  const dims = it.meta && it.meta.width ? `${it.meta.width}×${it.meta.height}` : "";
  const cam = it.meta && it.meta.model ? ((it.meta.make || "") + " " + it.meta.model).trim() : "";
  const gps = it.gps ? `<a href="https://www.openstreetmap.org/?mlat=${it.gps.lat}&mlon=${it.gps.lon}&zoom=14" target="_blank">${it.gps.lat.toFixed(5)}, ${it.gps.lon.toFixed(5)}</a>` : "";
  const dsrc = it.date && it.date_source ? `<span class="muted" style="font-size:11px"> · ${it.date_source}</span>` : "";
  // faces (detected) + manual person tags, unioned in one list
  const faceRows = (it.people || []).map(faceRow).join("") +
    (it.manual_people || []).map(manualPersonRow).join("");
  let faces;
  if (faceRows) faces = `<div class="facelist">${faceRows}</div>`;
  else if (it.type !== "image" && it.type !== "video")
    faces = `<div class="muted" style="font-size:12px">Face detection runs on photos and videos.</div>`;
  else faces = `<div class="muted" style="font-size:12px">No faces detected.</div>`;
  // pets (detected) + manual pet tags
  const animalRows = (it.animals || []).map(a => `<div class="facerow">
        <img class="facecrop" src="/animalThumb/${a.detection_id}" loading="lazy">
        <span>${a.name ? `<strong>${esc(a.name)}</strong> ` : ""}<span class="pet-species">${esc(a.species)}</span>
        <span class="muted">${Math.round(a.score * 100)}%</span></span></div>`).join("") +
    (it.manual_pets || []).map(manualPetRow).join("");
  const animals = animalRows ? `<div class="facelist">${animalRows}</div>`
    : `<div class="muted" style="font-size:12px">No pets detected.</div>`;
  // place
  const placeTxt = it.place ? (it.place.name ? esc(it.place.name) : '<span class="muted">Name this place</span>')
    : '<span class="muted">No place set</span>';
  document.getElementById("minfo").innerHTML = `<h3>${esc(it.name)}</h3>` +
    `<div class="isec"><div class="h">People <button class="linkbtn" onclick="addPersonPicker()">Add</button></div>
       <div id="people-add"></div>${faces}</div>` +
    `<div class="isec"><div class="h">Pets <button class="linkbtn" onclick="addPetPicker()">Add</button></div>
       <div id="pet-add"></div>${animals}</div>` +
    `<div class="isec"><div class="h">Place <button class="linkbtn" onclick="editPlace()">Change</button></div>
       <div id="placeval" class="val">${placeTxt}</div></div>` +
    `<div class="isec"><div class="h">Date <button class="linkbtn" onclick="editDate()">Edit</button></div>
       <div id="dateval" class="val">${fmtDate(it.date)}${dsrc}</div></div>` +
    `<div class="isec"><div class="h">Details</div>` +
    kv("Type", typeLabel(it.type)) + kv("Size", fmtBytes(it.size)) + kv("Dimensions", dims) + kv("Camera", cam) +
    kv("Coordinates", gps) + kv("Description", it.description ? esc(it.description) : "") + `</div>` +
    `<div class="isec"><div class="h">File</div>
       <div style="font-size:11px;color:var(--muted);word-break:break-all">${esc(it.rel_path)}</div>
       <div style="margin-top:10px"><a href="/file/${it.id}" target="_blank">Open original ↗</a></div></div>`;
}

/* ----- faces: reassign to a named person (pinned server-side) ----- */
// Optimistic saves: update the panel now, persist in the background, and roll back
// only if the DB write actually fails, so editing feels instant even while the
// pipeline holds the single writer. Every background callback bails out (or re-checks
// stillOpen) if the modal has since closed or moved to another item.
function faceRow(f) {
  const named = MITEM.person_options || [];
  const isNamed = f.person_id && f.name;
  let opts = isNamed ? "" : `<option value="" selected>${f.name ? esc(f.name) : "unknown"}</option>`;
  named.forEach(p => { opts += `<option value="${p.id}"${p.id === f.person_id ? " selected" : ""}>${esc(p.name)}</option>`; });
  if (!named.length && !isNamed)
    return `<div class="facerow"><img class="facecrop" src="/faceThumb/${f.face_id}" loading="lazy" onerror="this.style.visibility='hidden'">
      <span class="muted" style="font-size:12px">Name people in the People section to label them here.</span></div>`;
  return `<div class="facerow">
    <img class="facecrop" src="/faceThumb/${f.face_id}" loading="lazy" onerror="this.style.visibility='hidden'">
    <select class="fsel" title="Reassign this face" onchange="reassignFace(${f.face_id},this.value,this)">${opts}</select></div>`;
}

function stillOpen(id) { return MITEM && MITEM.id === id; }

function reassignFace(faceId, pid, sel) {
  if (!pid) return;
  const f = (MITEM.people || []).find(x => x.face_id === faceId);
  const prev = f ? { person_id: f.person_id, name: f.name } : null;
  const opt = (MITEM.person_options || []).find(p => p.id === +pid);
  if (f && opt) { f.person_id = opt.id; f.name = opt.name; }   // optimistic (the select already shows it)
  flashSaved(sel);
  const revert = (msg) => {
    if (f && prev) { f.person_id = prev.person_id; f.name = prev.name; }
    if (sel.isConnected) sel.value = (prev && prev.name) ? String(prev.person_id) : "";
    toast(msg, true);
  };
  qpost("/api/faces/reassign", { face_id: faceId, person_id: +pid })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t reassign that face: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t reassign that face: connection error"));
}
function flashSaved(el) {
  const o = el.style.borderColor; el.style.transition = "border-color .2s";
  el.style.borderColor = "var(--good)"; setTimeout(() => { el.style.borderColor = o; el.style.transition = ""; }, 900);
}

/* ----- manual people/pet tags: for media with no face/pet detected at all
   (back of a head, missed angle, group shot the detector skipped). Only
   named people/pets are offered (person_options/pet_options already
   filter to named ones), same discipline as reassignFace: mutate MITEM
   and repaint now, POST in the background, roll back + toast only on an
   actual failure, bail via stillOpen if the user has moved on. ----- */
function manualPersonRow(p) {
  return `<div class="facerow">
    <div class="facecrop placeholder">👤</div>
    <span style="flex:1;min-width:0"><strong>${esc(p.name)}</strong></span>
    <button class="linkbtn" onclick="removeManualPerson(${p.person_id})">Remove</button></div>`;
}
function manualPetRow(p) {
  return `<div class="facerow">
    <div class="facecrop placeholder">🐾</div>
    <span style="flex:1;min-width:0"><strong>${esc(p.name)}</strong></span>
    <button class="linkbtn" onclick="removeManualPet(${p.pet_id})">Remove</button></div>`;
}
function _taggedPersonIds(it) {
  const ids = new Set((it.people || []).filter(f => f.person_id).map(f => f.person_id));
  (it.manual_people || []).forEach(p => ids.add(p.person_id));
  return ids;
}
function _taggedPetIds(it) {
  const ids = new Set((it.animals || []).filter(a => a.pet_id).map(a => a.pet_id));
  (it.manual_pets || []).forEach(p => ids.add(p.pet_id));
  return ids;
}
function addPersonPicker() {
  const it = MITEM, host = document.getElementById("people-add");
  if (!host) return;
  if (!(it.person_options || []).length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Name people in the People section to label them here.</div>`;
    return;
  }
  const present = _taggedPersonIds(it);
  const avail = it.person_options.filter(p => !present.has(p.id));
  if (!avail.length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Everyone named is already tagged here.</div>`;
    return;
  }
  let sel = `<select class="fsel" onchange="onAddPerson(this.value)"><option value="" selected>Add a person…</option>`;
  avail.forEach(p => sel += `<option value="${p.id}">${esc(p.name)}</option>`);
  host.innerHTML = sel + `</select>`;
}
function onAddPerson(pid) {
  if (!pid) return;
  const it = MITEM, id = it.id;
  const opt = (it.person_options || []).find(p => p.id === +pid);
  if (!opt) return;
  it.manual_people = it.manual_people || [];
  it.manual_people.push({ person_id: opt.id, name: opt.name });
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) {
      it.manual_people = it.manual_people.filter(p => p.person_id !== opt.id);
      renderInfo();
    }
    toast(msg, true);
  };
  qpost("/api/item/person/add", { person_id: +pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t add that person: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t add that person: connection error"));
}
function removeManualPerson(pid) {
  const it = MITEM, id = it.id;
  const idx = (it.manual_people || []).findIndex(p => p.person_id === pid);
  if (idx < 0) return;
  const removed = it.manual_people[idx];
  it.manual_people.splice(idx, 1);
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) { it.manual_people.splice(idx, 0, removed); renderInfo(); }
    toast(msg, true);
  };
  qpost("/api/item/person/remove", { person_id: pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t remove that tag: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t remove that tag: connection error"));
}
function addPetPicker() {
  const it = MITEM, host = document.getElementById("pet-add");
  if (!host) return;
  if (!(it.pet_options || []).length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Name pets in the Pets section to label them here.</div>`;
    return;
  }
  const present = _taggedPetIds(it);
  const avail = it.pet_options.filter(p => !present.has(p.id));
  if (!avail.length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Every named pet is already tagged here.</div>`;
    return;
  }
  let sel = `<select class="fsel" onchange="onAddPet(this.value)"><option value="" selected>Add a pet…</option>`;
  avail.forEach(p => sel += `<option value="${p.id}">${esc(p.name)}</option>`);
  host.innerHTML = sel + `</select>`;
}
function onAddPet(pid) {
  if (!pid) return;
  const it = MITEM, id = it.id;
  const opt = (it.pet_options || []).find(p => p.id === +pid);
  if (!opt) return;
  it.manual_pets = it.manual_pets || [];
  it.manual_pets.push({ pet_id: opt.id, name: opt.name });
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) {
      it.manual_pets = it.manual_pets.filter(p => p.pet_id !== opt.id);
      renderInfo();
    }
    toast(msg, true);
  };
  qpost("/api/item/pet/add", { pet_id: +pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t add that pet: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t add that pet: connection error"));
}
function removeManualPet(pid) {
  const it = MITEM, id = it.id;
  const idx = (it.manual_pets || []).findIndex(p => p.pet_id === pid);
  if (idx < 0) return;
  const removed = it.manual_pets[idx];
  it.manual_pets.splice(idx, 1);
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) { it.manual_pets.splice(idx, 0, removed); renderInfo(); }
    toast(msg, true);
  };
  qpost("/api/item/pet/remove", { pet_id: pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t remove that tag: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t remove that tag: connection error"));
}

/* ----- date: variable precision (year / year-month / year-month-day) ----- */
function editDate() {
  const p = (MITEM.date || "").split("T")[0].split("-");
  document.getElementById("dateval").innerHTML = `
    <div class="dtrow">
      <input class="yr" id="d-y" type="text" inputmode="numeric" maxlength="4" placeholder="Year" value="${p[0] || ""}">
      <input id="d-m" type="text" inputmode="numeric" maxlength="2" placeholder="Mon" value="${p[1] ? (+p[1]) : ""}">
      <input id="d-d" type="text" inputmode="numeric" maxlength="2" placeholder="Day" value="${p[2] ? (+p[2]) : ""}">
    </div>
    <div class="muted" style="font-size:11px">Enter only what you know; year alone is fine.</div>
    <div class="btnrow"><button class="btn" onclick="saveDate()">Save</button>
      <button class="btn sec" onclick="renderInfo()">Cancel</button></div>`;
  document.getElementById("d-y").focus();
}
function saveDate() {
  const y = document.getElementById("d-y").value.trim(),
    mo = document.getElementById("d-m").value.trim(),
    da = document.getElementById("d-d").value.trim();
  if (!y) { toast("Year is required.", true); return; }
  const pad = v => String(v).padStart(2, "0");
  let v = String(+y);
  if (mo) { v += "-" + pad(+mo); if (da) v += "-" + pad(+da); }   // day needs a month
  const id = MITEM.id, prev = { date: MITEM.date, src: MITEM.date_source };
  MITEM.date = v; MITEM.date_source = "manual"; renderInfo();   // instant
  const revert = (msg) => {
    if (stillOpen(id)) { MITEM.date = prev.date; MITEM.date_source = prev.src; renderInfo(); }
    toast(msg, true);
  };
  qpost("/api/item/date", { file_id: id, datetime: v })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t save the date: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t save the date: connection error"));
}

/* ----- place: attach to a named place, or create one by pin ----- */
function editPlace() {
  const cur = MITEM.place ? MITEM.place.id : "";
  let sel = `<select class="fsel" onchange="onPlaceSelect(this.value)"><option value="">No place</option>`;
  (MITEM.place_options || []).forEach(p => sel += `<option value="${p.id}"${p.id === cur ? " selected" : ""}>${esc(p.name)}</option>`);
  sel += `</select>`;
  document.getElementById("placeval").innerHTML = sel +
    `<div class="btnrow"><button class="btn sec" onclick="newPlace()">＋ New place</button>
       <button class="linkbtn" onclick="renderInfo()">Done</button></div>
     <div id="p-pick"></div>`;
}
function onPlaceSelect(pid) {
  const id = MITEM.id, prev = MITEM.place;
  MITEM.place = pid ? ((MITEM.place_options || []).find(p => p.id === +pid) || { id: +pid, name: null }) : null;
  renderInfo();   // instant; collapses the editor back to display
  const body = pid ? { file_id: id, place_id: +pid } : { file_id: id, clear: true };
  const revert = (msg) => { if (stillOpen(id)) { MITEM.place = prev; renderInfo(); } toast(msg, true); };
  qpost("/api/item/place", body)
    .then(r => { if (!(r && r.ok)) revert("Couldn’t update the place: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t update the place: connection error"));
}
let MPICK = null, MPICK_TILES = null, MPICK_MARK = null, MPICK_LL = null;
function closePick() { if (MPICK) { MPICK.remove(); MPICK = null; } MPICK_TILES = null; MPICK_MARK = null; MPICK_LL = null; }
// The theme switch's one reach into the place picker's map, matching the
// Places map's own seam: the switch swaps tiles, it does not own the handle.
function syncPickerMapTiles() {
  if (MPICK) MPICK_TILES = replaceMapTiles(MPICK, MPICK_TILES);
}
function newPlace() {
  const host = document.getElementById("p-pick");
  host.innerHTML = `
    <input id="np-name" placeholder="Place name (e.g. Casa abuela)"
      style="width:100%;padding:7px 8px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);font:inherit;margin:8px 0 0;box-sizing:border-box">
    <div class="placepick" id="np-map"></div>
    <div class="muted" style="font-size:11px;margin:-2px 0 2px">Click the map to drop a pin; that becomes the place’s location.</div>
    <div class="btnrow"><button class="btn" id="np-save" onclick="saveNewPlace()" disabled>Create & attach</button>
      <button class="btn sec" onclick="closePick();editPlace()">Cancel</button></div>`;
  const start = MITEM.gps ? [MITEM.gps.lat, MITEM.gps.lon] : null;
  MPICK = L.map("np-map", {
    worldCopyJump: true, zoomSnap: 0,
    maxBounds: MAP_WORLD_BOUNDS, maxBoundsViscosity: 1
  });
  configureMapViewport(MPICK);
  MPICK_TILES = themedTileLayer().addTo(MPICK);
  if (start) { MPICK.setView(start, 15); dropPin(start[0], start[1]); } else MPICK.setView([20, 0], 1);
  MPICK.on("click", e => dropPin(e.latlng.lat, e.latlng.lng));
  setTimeout(() => MPICK && MPICK.invalidateSize(), 60);
}
function dropPin(lat, lon) {
  MPICK_LL = { lat, lon };
  if (MPICK_MARK) MPICK_MARK.setLatLng([lat, lon]);
  else MPICK_MARK = L.circleMarker([lat, lon], { radius: 8, weight: 2, color: "#fff", fillColor: "#3a7bd5", fillOpacity: 1 }).addTo(MPICK);
  const b = document.getElementById("np-save"); if (b) b.disabled = false;
}
function saveNewPlace() {
  if (!MPICK_LL) { toast("Click the map to set the location first.", true); return; }
  const id = MITEM.id, root = MITEM.root_id, prev = MITEM.place;
  const name = (document.getElementById("np-name").value || "").trim();
  const ll = { lat: MPICK_LL.lat, lon: MPICK_LL.lon };
  closePick();
  MITEM.place = { id: null, name: name || null }; renderInfo();   // instant (id filled in when it lands)
  qpost("/api/places/create", { root, name, lat: ll.lat, lon: ll.lon, file_id: id }).then(r => {
    if (r && r.ok) {
      if (stillOpen(id)) {
        MITEM.place = r.place;
        if (r.place && r.place.name) (MITEM.place_options = MITEM.place_options || []).push(r.place);
        renderInfo();
      }
    } else {
      if (stillOpen(id)) { MITEM.place = prev; renderInfo(); }
      toast("Couldn’t create the place: " + ((r && r.error) || "try again"), true);
    }
  }).catch(() => {
    if (stillOpen(id)) { MITEM.place = prev; renderInfo(); }
    toast("Couldn’t create the place: connection error", true);
  });
}

export function closeModal() {
  closePick(); document.getElementById("modal").classList.remove("open");
  document.getElementById("mmedia").innerHTML = ""; MITEM = null;
}
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
