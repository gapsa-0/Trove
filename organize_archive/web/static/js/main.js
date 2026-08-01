// The Trove frontend, entry point.
//
// Loaded as `<script type="module">`, so everything in here is module-scoped:
// nothing lands on `window` unless the export block at the bottom puts it there.
// That block is the one thing to read before changing anything above it.

import {
  esc, fmtBytes, fmtDate, setText, toast,
} from "./dom.js";
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
// Bumped on every user navigation (section switch / archive open). Async renders
// capture it and bail if it changed while they were awaiting, so a slow fetch can
// never paint a stale section over the one the user just picked.
export let NAV = 0;

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

function currentTheme() { return document.documentElement.dataset.theme === "dark" ? "dark" : "light"; }
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
const CARD_KIND = { scan: "scan", dedup: "dedup", detect: "detect", places: "places", semantic: "semantic" };
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
function renderGstat(snap) {
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
function renderNav() {
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
let ACTIVE_SECTION = null;
function resetSectionViews() {
  const main = document.getElementById("main");
  if (MAP) { MAP.remove(); MAP = null; MAP_LAYER = null; MAP_TILES = null; }
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
function showSection(id, reload = false) {
  if (!RENDERERS[id]) id = "overview";
  if (ACTIVE_SECTION === id && !reload) return;
  NAV++; const gen = NAV;
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
    if (gen === NAV && ACTIVE_SECTION === id) SECTION_READY.add(id);
  }).catch(err => {
    if (gen !== NAV) return;
    console.error("section render failed:", id, err);
    m.innerHTML = `<div class="soonbox"><div class="big">⚠️</div>
      <p>Couldn't load this section.</p>
      <p class="muted">${(err && err.message) || err}</p>
      <p style="margin-top:14px"><button class="btn sec" onclick="showSection('${id}',true)">Retry</button></p></div>`;
    SECTION_READY.add(id);
  });
}

/* ---------- overview + tasks ---------- */
async function renderOverview(m) {
  const gen = NAV, root = S.arch.id;
  const [s, ds, fs, ps, ss] = await Promise.all([
    jget("/api/summary?root=" + root),
    jget("/api/dups/summary?root=" + root),
    jget("/api/faces/summary?root=" + root),
    jget("/api/pets/summary?root=" + root).catch(() => null),
    jget("/api/browse/semantic/status?root=" + root).catch(() => null)]);
  if (gen !== NAV) return;   // user switched sections while these were loading
  S.dupsum = ds;
  S.facesum = fs;
  S.petsum = ps;
  S.semanticsum = ss;
  m.innerHTML = `<div class="pagehead">
      <div><h2 class="sec">Library overview</h2>
      <p>Everything important about this archive, at a glance.</p></div>
    </div>
    <div class="statrow">
      <button class="stat" onclick="showSection('library')"><span class="metric-icon blue">${ICONS.library}</span><div><div class="k">All media</div><div class="v" id="ov-total">${s.total.toLocaleString()}</div></div></button>
      <button class="stat" onclick="showSection('timeline')"><span class="metric-icon violet">${ICONS.timeline}</span><div><div class="k">With a date</div><div class="v" id="ov-enriched">${s.enriched.toLocaleString()}</div></div></button>
      <button class="stat" onclick="showSection('places')"><span class="metric-icon green">${ICONS.places}</span><div><div class="k">With a location</div><div class="v" id="ov-gps">${s.with_gps.toLocaleString()}</div></div></button>
      <button class="stat" onclick="showSection('dups')"><span class="metric-icon orange">${ICONS.dups}</span><div><div class="k">Duplicate copies</div><div class="v" id="ov-dups">${ds.duplicates.toLocaleString()}</div></div></button>
    </div>
    <div class="overview-grid">
      <div class="panel status-panel"><div class="panel-heading"><span class="panel-symbol">${ICONS.overview}</span><div><h3>Library health</h3><p>Scanning, metadata, faces, and duplicate analysis</p></div><button type="button" class="btn sec pause-btn" id="pause-btn" onclick="togglePipelinePause()">Pause all</button></div>
        <p class="pause-note" id="pause-note" style="display:none">Paused — background processing is stopped.</p>
        <div id="syncstatus"></div><div id="jobarea"></div>
      </div>
      <div class="panel type-panel"><div class="panel-heading"><div><h3>Storage</h3><p id="ov-storage-caption">${fmtBytes(s.size)} across ${s.total.toLocaleString()} items</p></div><div class="metric-switch" id="storage-switch"></div></div>
        <div class="storage-bar" id="typebar"></div><table class="types" id="typetbl"></table>
      </div>
    </div>`;
  S.summary = s;
  renderStoragePanel(s);   // reads S.summary back when the metric switch flips
  renderHealthCards();   // paint immediately from the last snapshot (if any)
  startPoll();           // single poller: /api/pipeline drives every status surface
}

// Fills the "Storage" panel (byte total + one bar + table) from an
// /api/summary payload. Called from renderOverview on first paint AND from
// refreshPipeline on every poll tick while the pipeline is busy, so bytes
// reclaimed/added by scan+dedup show up live instead of only at the
// busy→idle edge (the caption, switch, bar and table share stable ids).
//
// Files and size are two readings of the same breakdown and they disagree
// sharply -- in a photo archive the videos are a rounding error by count and
// most of the disk. Showing both at once meant two bars and a legend of
// percentages nobody asked for. One bar answers whichever question the
// switch is set to; the exact numbers live in the table, where numbers
// belong, and the per-type detail appears on hover.
const STORAGE_METRICS = {
  size: {
    label: "Size", of: "of size",
    value: t => t.size || 0,
    text: t => fmtBytes(t.size || 0),
  },
  files: {
    label: "Files", of: "of files",
    value: t => t.count,
    text: t => `${t.count.toLocaleString()} file${t.count === 1 ? "" : "s"}`,
  },
};
function storageMetric() {
  return STORAGE_METRICS[S.storageMetric] ? S.storageMetric : "size";
}
function setStorageMetric(metric) {
  S.storageMetric = metric;
  if (S.summary) renderStoragePanel(S.summary);
}
function renderStoragePanel(s) {
  const cap = document.getElementById("ov-storage-caption");
  if (cap) cap.textContent = `${fmtBytes(s.size)} across ${s.total.toLocaleString()} items`;
  const key = storageMetric(), metric = STORAGE_METRICS[key];
  const colour = t => TYPE_COL[t.type] || TYPE_COL.other;
  // Sorted by the metric on screen, so the bar and the table below it read
  // in the same order whichever way the switch is set.
  const types = s.types.filter(t => t.count > 0)
    .sort((a, b) => metric.value(b) - metric.value(a));
  const total = types.reduce((sum, t) => sum + metric.value(t), 0) || 1;
  // A type can be a real part of the archive and still round to 0.0%
  // (30 files, 98.7 GB): "<0.1%" is honest where "0.0%" reads as nothing.
  const share = t => {
    const p = 100 * metric.value(t) / total;
    return p > 0 && p < 0.1 ? "<0.1%" : `${p.toFixed(1)}%`;
  };

  const sw = document.getElementById("storage-switch");
  if (sw) sw.innerHTML = Object.entries(STORAGE_METRICS).map(([id, m]) =>
    `<button type="button" class="${id === key ? "on" : ""}"
        onclick="setStorageMetric('${id}')">${m.label}</button>`).join("");

  const typebar = document.getElementById("typebar");
  if (typebar) {
    typebar.innerHTML = `<div class="type-summary-bar">${types.map((t, i) =>
      `<div class="type-summary-segment" data-i="${i}" style="width:${100 * metric.value(t) / total}%;background:${colour(t)}"></div>`
    ).join("")}</div><div class="storage-tip" hidden></div>`;
    const tip = typebar.querySelector(".storage-tip");
    typebar.querySelectorAll(".type-summary-segment").forEach(seg => {
      seg.onmouseenter = () => {
        const t = types[+seg.dataset.i];
        tip.innerHTML = `<span class="swatch" style="background:${colour(t)}"></span>${typeLabel(t.type)} <span class="muted">· ${metric.text(t)} · ${share(t)} ${metric.of}</span>`;
        // Centred on the segment, then held inside the panel so a sliver at
        // either end doesn't push the tooltip off the edge.
        const mid = seg.offsetLeft + seg.offsetWidth / 2;
        tip.hidden = false;
        const half = tip.offsetWidth / 2;
        tip.style.left =
          Math.max(half, Math.min(typebar.clientWidth - half, mid)) + "px";
      };
      seg.onmouseleave = () => { tip.hidden = true; };
    });
  }

  const tbl = document.getElementById("typetbl");
  if (!tbl) return;
  tbl.innerHTML = `<tr><th>Type</th><th>Files</th><th>Size</th><th>Share</th></tr>`;
  types.forEach(t => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="swatch" style="background:${colour(t)}"></span>${typeLabel(t.type)}</td>
      <td class="num">${t.count.toLocaleString()}</td>
      <td class="num">${fmtBytes(t.size)}</td>
      <td class="num share">${share(t)}</td>`;
    tbl.appendChild(tr);
  });
}

// Everything runs automatically in the background; this panel only reports
// what's happening. Every card is rendered the SAME way from one resolved
// state the server computes (running/queued/blocked/up_to_date/unavailable/
// error), no per-card logic here, so the six cards can never disagree with
// each other or with what the pipeline is actually doing.
const HEALTH_CARDCLASS = { running: "running", queued: "pending", blocked: "", up_to_date: "", unavailable: "", error: "error" };
const HEALTH_DOT = { queued: "pending", blocked: "check", up_to_date: "ok", unavailable: "check", error: "check" };

function healthDoneMessage(id) {
  // The "done" (up_to_date) line reuses the per-domain summary numbers the
  // Overview already holds, so it reads as a result, not a bare "done".
  const s = S.summary, ds = S.dupsum, fs = S.facesum, ps = S.petsum, ss = S.semanticsum;
  switch (id) {
    case "scan": return `${(s && s.total || 0).toLocaleString()} files catalogued`;
    case "dedup": return ds && ds.duplicates
      ? `${ds.duplicates.toLocaleString()} redundant cop${ds.duplicates === 1 ? "y" : "ies"} found`
      : "No redundant copies found";
    case "detect": {
      const parts = [];
      if (fs && fs.faces) parts.push(`${fs.faces.toLocaleString()} face${fs.faces === 1 ? "" : "s"}`);
      if (ps && ps.detections) parts.push(`${ps.detections.toLocaleString()} animal${ps.detections === 1 ? "" : "s"}`);
      return parts.length ? parts.join(" · ")
        : `${(fs && fs.scanned || 0).toLocaleString()} photo${fs && fs.scanned === 1 ? "" : "s"} analyzed`;
    }
    case "places": return s && s.with_gps
      ? `${s.with_gps.toLocaleString()} location${s.with_gps === 1 ? "" : "s"} mapped`
      : "No locations found";
    case "semantic": return ss && ss.indexed
      ? `${ss.indexed.toLocaleString()} item${ss.indexed === 1 ? "" : "s"} indexed`
      : (ss && ss.configured ? "Ready to index" : "Not configured");
    default: return "";
  }
}

function healthCard(stage) {
  const st = stage.state;
  // Paused cards should read as stopped, not "about to run": swap the amber
  // pending dot for the same neutral one blocked cards use. `stalled` covers
  // both a stage the user paused and one queued behind a paused stage --
  // neither is going to start. A genuinely still-running (winding-down)
  // stage is untouched here -- its dot/spinner already come from `st`.
  const stopped = (!!(S.pipeline && S.pipeline.paused) || stage.stalled) && st !== "running";
  const dot = stage.next ? "next" : (stopped ? "check" : (HEALTH_DOT[st] || "check"));
  const head = (st === "running"
    ? `<span class="spin"></span>${stage.label}`
    : `<span class="dot ${dot}"></span>${stage.label}`) + stagePauseButton(stage);
  const message = st === "up_to_date" ? healthDoneMessage(stage.id) : (stage.message || "");
  let prog = "";
  const p = stage.progress;
  if (st === "running" && p && (p.total || p.done)) {
    const pct = Math.max(0, Math.min(100, p.percent != null ? p.percent : 0));
    prog = `<div class="job" style="margin-top:8px; border:none; padding:0; background:transparent"><div class="progress"><div class="progfill" style="width:${pct}%"></div></div>
        <div class="cur" style="margin-top:4px">${pct}% · ${(p.done || 0).toLocaleString()}${p.total ? "/" + p.total.toLocaleString() : ""} · ${p.elapsed || 0}s${p.current ? " · " + p.current : ""}</div></div>`;
  }
  // Beyond pause/resume no card offers a call to action. Semantic indexing
  // was the only one that did ("Add API key"), and it has nothing left to
  // configure: it is unavailable only when the app was installed without its
  // optional dependencies, which no button here could fix.
  const cls = stopped ? "paused" : (stage.next ? "next" : (HEALTH_CARDCLASS[st] || ""));
  return `<div class="health-task ${cls}">
      <div class="health-task-head">${head}</div>
      <div class="health-task-state">${message}${prog}</div></div>`;
}

// Per-stage pause/resume (things_to_fix #32). One button per card, on top of
// the whole-pipeline switch in the panel heading: it stops just this stage
// and lets the others carry on. Deliberately inert while the whole pipeline
// is paused -- nothing is running, so there is nothing here to stop.
const PAUSE_GLYPH = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="5" width="4" height="14" rx="1.2" fill="currentColor"/><rect x="13" y="5" width="4" height="14" rx="1.2" fill="currentColor"/></svg>';
const PLAY_GLYPH = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5Z" fill="currentColor"/></svg>';
function stagePauseButton(stage) {
  // An unavailable stage has no work to pause (its backend isn't installed),
  // and the non-stage jobs in `extra` are user-kicked one-offs, not stages.
  if (stage.state === "unavailable" || !CARD_KIND[stage.id]) return "";
  const globallyPaused = !!(S.pipeline && S.pipeline.paused);
  const off = !!stage.paused;
  const label = off ? `Resume ${stage.label}` : `Pause ${stage.label}`;
  const title = globallyPaused
    ? "All background processing is paused" : label;
  return `<button type="button" class="health-task-btn" title="${title}"
      aria-label="${label}" ${globallyPaused || S.pausing ? "disabled" : ""}
      onclick="toggleStagePause('${stage.id}',event)">${off ? PLAY_GLYPH : PAUSE_GLYPH}</button>`;
}

async function toggleStagePause(id, event) {
  if (event) event.stopPropagation();
  if (!S.arch || S.pausing) return;
  const stage = (S.pipeline && S.pipeline.stages || []).find(s => s.id === id);
  if (!stage) return;
  const next = !stage.paused;
  // Paint the new state at once (the poll is up to 1.2s away), then let
  // refreshPipeline replace it with whatever the server actually did.
  stage.paused = next;
  S.pausing = true;
  renderHealthCards();
  try {
    const r = await jpost("/api/pipeline/pause", { paused: next, stage: id });
    if (r && r.error) toast(r.error, true);
  } catch {
    toast(next ? "Could not pause this step." : "Could not resume this step.", true);
  } finally { S.pausing = false; }
  await refreshPipeline();
}

function renderHealthCards() {
  const el = document.getElementById("syncstatus"); if (!el) return;
  const snap = S.pipeline;
  renderPauseControl();
  if (!snap || !snap.stages) {
    el.innerHTML = `<div class="health-grid"><div class="health-task running"><div class="health-task-head"><span class="spin"></span>Checking for work…</div></div></div>`;
    return;
  }
  el.innerHTML = `<div class="health-grid">${snap.stages.map(healthCard).join("")}</div>`;
}

// Whole-pipeline pause/resume (things_to_fix #19): pausing cancels every
// running job at its next batch checkpoint (see JobManager.set_paused), and
// the scheduler simply stops starting new ones until resumed. Individual
// stages have their own buttons on the cards (stagePauseButton); this one
// outranks them, which is why it disables them while it is on.
function renderPauseControl() {
  const btn = document.getElementById("pause-btn");
  const note = document.getElementById("pause-note");
  const paused = !!(S.pipeline && S.pipeline.paused);
  if (btn) { btn.textContent = paused ? "Resume all" : "Pause all"; btn.disabled = !!S.pausing; }
  if (note) note.style.display = paused ? "" : "none";
}

async function togglePipelinePause() {
  if (!S.arch || S.pausing) return;
  const next = !(S.pipeline && S.pipeline.paused);
  S.pausing = true;
  renderPauseControl();
  try {
    const r = await jpost("/api/pipeline/pause", { paused: next });
    if (r && r.error) toast(r.error, true);
  } catch { toast(next ? "Could not pause processing." : "Could not resume processing.", true); }
  finally { S.pausing = false; }
  await refreshPipeline();
}

// The one poller behind every status surface: fetch the snapshot, render the
// cards + sidebar chip, and keep the top stat numbers climbing while active.
async function refreshPipeline() {
  if (!S.arch) { stopPoll(); return; }
  let snap;
  try { snap = await jget("/api/pipeline?root=" + S.arch.id); }
  catch (e) { return; }   // transient; the next tick retries
  S.pipeline = snap;
  renderHealthCards();
  renderGstat(snap);
  const area = document.getElementById("jobarea"); if (area) area.innerHTML = "";
  // Paused counts as not busy: nothing can change, so don't keep re-running
  // the summary/duplicate queries behind the user's back.
  const busy = snap.overall !== "idle" && !snap.paused;
  if (busy && S.section === "overview") {
    // Numbers should climb live while work runs, without waiting for the
    // whole pipeline to finish.
    try {
      const [s, ds] = await Promise.all([
        jget("/api/summary?root=" + S.arch.id),
        jget("/api/dups/summary?root=" + S.arch.id)]);
      S.summary = s; S.dupsum = ds;
      const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
      set("ov-total", s.total.toLocaleString());
      set("ov-enriched", s.enriched.toLocaleString());
      set("ov-gps", s.with_gps.toLocaleString());
      set("ov-dups", ds.duplicates.toLocaleString());
      renderStoragePanel(s);
    } catch (_) { /* non-critical; next tick retries */ }
  }
  // On the busy→idle edge, re-render the Overview once so the "done" messages
  // pick up the final summary numbers. Guarding on the transition avoids an
  // endless renderOverview→startPoll→refresh loop.
  const wasBusy = S.pipeActive; S.pipeActive = busy;
  if (wasBusy && !busy && S.section === "overview") {
    renderOverview(document.getElementById("main"));
  }
}
function startPoll() { stopPoll(); S.poll = setInterval(refreshPipeline, 1200); refreshPipeline(); }
function stopPoll() { if (S.poll) { clearInterval(S.poll); S.poll = null; } }

/* ---------- date sources (complementary bar shown under the Timeline) ---------- */
const DATE_SRC_LABEL = {
  takeout_json: "Google Takeout JSON", exif: "Embedded EXIF",
  filename: "Filename pattern", mtime: "File modified time", unknown: "Unresolved source",
  unresolved: "No date found"
};
const DATE_SRC_COL = {
  takeout_json: "#5b9dff", exif: "#c77dff", filename: "#57c98b",
  mtime: "#e6b45e", unknown: "#9aa3b2", unresolved: "#4a5261"
};
async function renderDateSourceBar() {
  const el = document.getElementById("dsbar"); if (!el) return;
  const r = await jget("/api/dates/sources?root=" + S.arch.id);
  const rows = r.sources.slice();
  if (r.undated > 0) rows.push({ source: "unresolved", count: r.undated });
  const total = r.total || 1;
  const segs = rows.map(x =>
    `<div style="width:${100 * x.count / total}%;background:${DATE_SRC_COL[x.source] || "#8b93a3"}" title="${DATE_SRC_LABEL[x.source] || x.source}: ${(100 * x.count / total).toFixed(1)}%"></div>`
  ).join("");
  const legend = rows.map(x =>
    `<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${DATE_SRC_COL[x.source] || "#8b93a3"};margin-right:6px"></span>${DATE_SRC_LABEL[x.source] || x.source} <span class="muted">, ${(100 * x.count / total).toFixed(1)}%</span></span>`
  ).join("");
  el.innerHTML = `<div style="display:flex;height:14px;border-radius:7px;overflow:hidden">${segs}</div>
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:12px">${legend}</div>`;
}

/* ---------- timeline ---------- */
const TL_COL = TYPE_COL;
async function renderTimeline(m) {
  const gen = NAV;
  S.timeline = { bucket: "month", year: "", month: "", people: [], place: "" };
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Timeline</h2>
      <p>See how your archive grows over time, then narrow it by date, people together, or place.</p></div></div>
    <div class="filterbar" id="tl-filterbar"></div>
    <div id="tllegend" style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:8px;font-size:12px"></div>
    <canvas id="tlc2" width="1180" height="380"></canvas>
    <div class="muted" style="margin-top:8px;font-size:12px">Running total of files over time, each type scaled to its own final count.</div>
    <h2 class="sec" style="margin-top:28px">How dates were found</h2>
    <div class="panel" id="dsbar">Loading…</div>`;
  await buildTimelineFilterBar();
  if (gen !== NAV) return;
  await Promise.all([drawTimeline("month"), renderDateSourceBar()]);
}
async function buildTimelineFilterBar() {
  const gen = NAV;
  const f = await jget("/api/browse/filters?root=" + S.arch.id);
  if (gen !== NAV) return;
  const bar = document.getElementById("tl-filterbar"); if (!bar) return;
  const years = [...new Set((f.periods || []).map(p => p.slice(0, 4)))];
  const opt = (v, l) => `<option value="${v}">${l}</option>`;
  const parts = [];
  if (years.length)
    parts.push(`<select class="fsel" id="tl-year-filter" onchange="onTimelineYearChange()">` +
      opt("", "All years") + years.map(y => opt(y, y)).join("") + `</select>` +
      `<select class="fsel" id="tl-month-filter" onchange="applyTimelineFilters()" disabled>` +
      opt("", "All months") + `</select>`);
  parts.push(peopleFilterHTML("tl", f.people || []));
  parts.push(`<select class="fsel" id="tl-place-filter" onchange="applyTimelineFilters()" ${f.places && f.places.length ? "" : "disabled"} title="${f.places && f.places.length ? "Filter by place" : "Name places in Places to enable this filter"}">` +
    opt("", f.places && f.places.length ? "All places" : "No places named yet") + (f.places || []).map(p => opt(p.id, esc(p.name))).join("") + `</select>`);
  parts.push(`<button class="linkbtn" id="tl-clear" onclick="clearTimelineFilters()" style="display:none">Clear filters</button>`);
  bar.innerHTML = parts.join("");
  S.timelineOpts = f;
}
function onTimelineYearChange() {
  const year = selVal("tl-year-filter");
  const msel = document.getElementById("tl-month-filter");
  if (msel) {
    if (!year) { msel.innerHTML = '<option value="">All months</option>'; msel.disabled = true; }
    else {
      const months = [...new Set((S.timelineOpts.periods || [])
        .filter(p => p.slice(0, 4) === year).map(p => p.slice(5, 7)).filter(Boolean))].sort();
      msel.innerHTML = '<option value="">All months</option>' +
        months.map(mm => `<option value="${mm}">${MONTH_NAMES[+mm - 1]}</option>`).join("");
      msel.disabled = false;
    }
  }
  applyTimelineFilters();
}
function applyTimelineFilters() {
  const t = S.timeline;
  t.year = selVal("tl-year-filter");
  const mm = selVal("tl-month-filter");
  t.month = (t.year && mm) ? `${t.year}-${mm}` : "";
  t.people = checkedPeople("tl");
  t.place = selVal("tl-place-filter");
  updatePeopleFilterLabel("tl", S.timelineOpts.people || []);
  const clear = document.getElementById("tl-clear");
  if (clear) clear.style.display = (t.year || t.people.length || t.place) ? "" : "none";
  drawTimeline(t.bucket);
}
function clearTimelineFilters() {
  ["tl-year-filter", "tl-place-filter"].forEach(id => {
    const e = document.getElementById(id); if (e) e.value = "";
  });
  clearPeopleChecks("tl");
  const msel = document.getElementById("tl-month-filter");
  if (msel) { msel.innerHTML = '<option value="">All months</option>'; msel.disabled = true; }
  applyTimelineFilters();
}
function monotonePath(ctx, xs, ys) {
  // Fritsch-Carlson monotone cubic: smooth but never overshoots the data,
  // so a line can't dip below zero or spike between points.
  const n = xs.length;
  if (n === 1) { return; }
  const dx = [], slope = [];
  for (let i = 0; i < n - 1; i++) { dx[i] = xs[i + 1] - xs[i]; slope[i] = (ys[i + 1] - ys[i]) / dx[i]; }
  const t = new Array(n); t[0] = slope[0]; t[n - 1] = slope[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = (slope[i - 1] * slope[i] <= 0) ? 0 : (slope[i - 1] + slope[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / slope[i], b = t[i + 1] / slope[i], h = Math.hypot(a, b);
    if (h > 3) { const tau = 3 / h; t[i] = tau * a * slope[i]; t[i + 1] = tau * b * slope[i]; }
  }
  ctx.moveTo(xs[0], ys[0]);
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i];
    ctx.bezierCurveTo(xs[i] + h / 3, ys[i] + t[i] * h / 3, xs[i + 1] - h / 3, ys[i + 1] - t[i + 1] * h / 3, xs[i + 1], ys[i + 1]);
  }
}
// Draws one type's curve normalized to its own max (0..1), so each type's
// shape is comparable regardless of volume. Per-type totals live in the
// legend above the canvas, not inside the plot.
function drawTypeChart(canvasId, rows, ordered, maxByType) {
  const cv = document.getElementById(canvasId); if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, padL = 40, padR = 16, padB = 28, padT = 14;
  ctx.clearRect(0, 0, W, H);
  const n = rows.length;
  const X = i => padL + (n === 1 ? (W - padL - padR) / 2 : i * (W - padL - padR) / (n - 1));
  const Y = frac => H - padB - frac * (H - padT - padB);
  [0, 0.25, 0.5, 0.75, 1].forEach(frac => {
    const y = Y(frac);
    ctx.strokeStyle = frac === 0 ? "#3a414e" : "#232833";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
  });
  ctx.font = "10px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.fillStyle = "#9aa3b2"; ctx.fillText("0", padL - 8, Y(0));
  ordered.forEach(t => {
    const xs = rows.map((r, i) => X(i)), ys = rows.map(r => Y((r[t] || 0) / (maxByType[t] || 1)));
    if (n === 1) { ctx.fillStyle = TL_COL[t]; ctx.beginPath(); ctx.arc(xs[0], ys[0], 3, 0, 7); ctx.fill(); return; }
    ctx.beginPath(); monotonePath(ctx, xs, ys);
    ctx.lineTo(xs[n - 1], H - padB); ctx.lineTo(xs[0], H - padB); ctx.closePath();
    ctx.fillStyle = TL_COL[t] + "1e"; ctx.fill();
    ctx.beginPath(); monotonePath(ctx, xs, ys);
    ctx.strokeStyle = TL_COL[t]; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
  });
  ctx.fillStyle = "#9aa3b2"; ctx.textAlign = "center"; ctx.textBaseline = "top";
  const step = Math.max(1, Math.ceil(n / 12));
  rows.forEach((r, i) => { if (i % step === 0 || i === n - 1) ctx.fillText(r.period, X(i), H - padB + 8); });
}
function drawTypeLegend(legendId, ordered, totalsByType) {
  const leg = document.getElementById(legendId); leg.innerHTML = "";
  ordered.forEach(t => {
    const sp = document.createElement("span");
    sp.innerHTML = `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${TL_COL[t]};margin-right:6px"></span>` +
      t + ` <span class="muted">, ${totalsByType[t].toLocaleString()}</span>`;
    leg.appendChild(sp);
  });
}
async function drawTimeline(bucket) {
  const t = S.timeline;
  t.bucket = bucket;
  const p = new URLSearchParams({ root: S.arch.id, bucket });
  if (t.year) p.set("year", t.year); if (t.month) p.set("month", t.month);
  t.people.forEach(id => p.append("person", id)); if (t.place) p.set("place", t.place);
  const { series } = await jget("/api/timeline?" + p);
  const cv2 = document.getElementById("tlc2");
  const leg = document.getElementById("tllegend");
  if (!cv2) return;
  if (!series.length) {
    cv2.getContext("2d").clearRect(0, 0, cv2.width, cv2.height);
    const filtered = t.year || t.people.length || t.place;
    leg.innerHTML = `<span class="muted">${filtered ? "No dated media matches these filters." : "No dated media yet, run Extract on the Overview tab."}</span>`;
    return;
  }
  const types = ["image", "video", "audio"].filter(t => series.some(s => s[t]));
  const totalsByType = {};
  types.forEach(t => { totalsByType[t] = series.reduce((a, s) => a + (s[t] || 0), 0); });
  const ordered = types.slice().sort((a, b) => totalsByType[b] - totalsByType[a]);

  const running = Object.fromEntries(types.map(t => [t, 0]));
  const cumRows = series.map(s => {
    const row = { period: s.period };
    types.forEach(t => { running[t] += s[t] || 0; row[t] = running[t]; });
    return row;
  });
  const cumMax = {}; types.forEach(t => { cumMax[t] = Math.max(totalsByType[t], 1); });
  drawTypeChart("tlc2", cumRows, ordered, cumMax);
  drawTypeLegend("tllegend", ordered, totalsByType);
}

/* ---------- map (Leaflet: place clusters over OpenStreetMap) ----------
   Photos within 300m of each other are grouped server-side into one named
   "place" (organize_archive/geo/clusters.py). The screen-pixel bucketing
   below is a second, purely visual layer on top of that: at far zoom it
   still merges nearby PLACES into one numbered bubble; at close zoom each
   place stands alone as a small thumbnail collage + name. ---------- */
const MAP_TILE_STYLES = {
  light: {
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    options: { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }
  },
  dark: {
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    options: { maxZoom: 20, subdomains: "abcd", attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>' }
  }
};
const MAP_WORLD_BOUNDS = L.latLngBounds(
  [[-85.0511287798, -1000000], [85.0511287798, 1000000]]);
function themedTileLayer() {
  const style = MAP_TILE_STYLES[currentTheme()];
  return L.tileLayer(style.url, style.options);
}
function syncMapZoomFloor(map) {
  if (!map) return;
  // A Web Mercator world is 256 px tall at zoom 0. This fractional zoom
  // makes it exactly as tall as the map, so zooming out can never expose
  // empty space beyond its north or south edge.
  const minZoom = Math.log(map.getSize().y / 256) / Math.LN2;
  map.setMinZoom(minZoom);
}
function configureMapViewport(map) {
  syncMapZoomFloor(map);
  map.on("resize", () => syncMapZoomFloor(map));
}
function replaceMapTiles(map, tiles) {
  if (!map) return tiles;
  if (tiles) map.removeLayer(tiles);
  return themedTileLayer().addTo(map);
}
function syncMapTiles() {
  if (MAP) MAP_TILES = replaceMapTiles(MAP, MAP_TILES);
  if (MPICK) MPICK_TILES = replaceMapTiles(MPICK, MPICK_TILES);
}
let MAP = null, MAP_LAYER = null, MAP_TILES = null, MAP_CLUSTERS = [], MAP_HIDDEN = {};
// Un-clustered view (things_to_fix #33): every geotagged file as its own
// point. Fetched once, lazily, the first time the user asks for it -- it is
// a much bigger payload than the ~hundreds of place centroids, and most
// visits never leave the clustered view.
let MAP_POINTS = null, MAP_POINTS_UNPLACED = 0, MAP_POINT_CANVAS = null;
// The built point layer and what it was built from (see showPhotoPoints).
let MAP_POINT_LAYER = null, MAP_POINT_BUILT = null;
async function renderMap(m) {
  const gen = NAV, root = S.arch.id;
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Places</h2>
      <p>Explore geolocated media and give meaningful names to the places you return to.</p></div></div>
    <div class="statrow map-stats">
      <div class="stat"><div><div class="k">Photos in places</div><div class="v" id="map-photo-count">-</div></div></div>
      <div class="stat"><div><div class="k">Places</div><div class="v" id="map-place-count">-</div></div></div>
      <div class="stat"><div><div class="k">Named places</div><div class="v" id="map-named-count">-</div></div></div>
    </div>
    <div class="mapwrap">
      <div id="lmap"></div>
      <div id="mapside"></div>
    </div>
    <div class="map-footnote" id="map-view-note"></div>
    <div class="map-footnote" id="map-hidden-note" hidden></div>
    <div id="placegallery"></div>`;
  if (MAP) { MAP.remove(); MAP = null; MAP_LAYER = null; }
  MAP_POINT_CANVAS = null; MAP_POINT_LAYER = null; MAP_POINT_BUILT = null;
  S.mapSel = null;
  // The view choice is a preference and survives; the points themselves are
  // this archive's data and are re-fetched on demand (see setMapView).
  S.mapView = S.mapView || "places";
  MAP_POINTS = null; MAP_POINTS_UNPLACED = 0;
  const { clusters, hidden } = await jget("/api/map/clusters?root=" + root);
  if (gen !== NAV) return;
  MAP_CLUSTERS = clusters;
  MAP_HIDDEN = hidden || {};
  updateMapStats();
  renderPlaceGallery();
  MAP = L.map("lmap", {
    worldCopyJump: true, zoomSnap: 0,
    maxBounds: MAP_WORLD_BOUNDS, maxBoundsViscosity: 1
  });
  configureMapViewport(MAP);
  MAP_TILES = themedTileLayer().addTo(MAP);
  if (!clusters.length) {
    MAP.setView([0, 0], 2);
    renderMapViewNote();
    document.querySelector(".mapwrap").insertAdjacentHTML("beforeend", `<div class="map-empty">
      <div class="big">⌖</div><h3>No locations yet</h3>
      <p>Places will appear here automatically when Archive finds GPS information in EXIF or Takeout metadata.</p></div>`);
    return;
  }
  addMapViewToggle(MAP);
  const b = L.latLngBounds(clusters.map(c => [c.lat, c.lon]));
  MAP.fitBounds(b, { padding: [40, 40], maxZoom: 14 });
  MAP.on("moveend zoomend", drawMap);
  drawMap();
  renderMapViewNote();
}

/* -- Places / Photos switch -------------------------------------------
   Two honest answers to different questions: the clustered view says where
   this family keeps going back to (one marker per place, however far its
   members are spread); the un-clustered one says where each photo was
   actually taken, which a centroid necessarily hides. */
function addMapViewToggle(map) {
  const Toggle = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const box = L.DomUtil.create("div", "map-viewtoggle");
      box.id = "map-viewtoggle";
      // Without this a click on the switch also reaches the map underneath
      // (and a double click zooms it).
      L.DomEvent.disableClickPropagation(box);
      return box;
    }
  });
  map.addControl(new Toggle());
  renderMapViewToggle();
}
function renderMapViewToggle(loading) {
  const box = document.getElementById("map-viewtoggle"); if (!box) return;
  const btn = (view, label) =>
    `<button type="button" class="${S.mapView === view ? "on" : ""}"
        ${loading ? "disabled" : ""} onclick="setMapView('${view}')">${label}</button>`;
  box.innerHTML = btn("places", "Places") + btn("photos", "Photos");
}
async function setMapView(view) {
  if (!MAP || S.mapView === view) return;
  S.mapView = view;
  renderMapViewToggle(view === "photos" && !MAP_POINTS);
  if (view === "photos" && !MAP_POINTS) {
    try {
      await loadMapPoints();
    } catch {
      toast("Couldn’t load the individual photo locations.", true);
      S.mapView = "places";
    }
    if (S.section !== "places" || !MAP) return;   // user navigated away meanwhile
  }
  renderMapViewToggle();
  drawMap();
  renderMapViewNote();
}
async function loadMapPoints() {
  const r = await jget("/api/map/points?root=" + S.arch.id);
  MAP_POINTS = r.points || [];
  MAP_POINTS_UNPLACED = r.unplaced || 0;
}
// Anything that changes which places exist or which files they hold also
// changes a point's colour (naming a one-off spot promotes it out of grey,
// merging moves files between hues), so the cache is dropped and only
// re-pulled when that view is actually on screen.
async function invalidateMapPoints() {
  MAP_POINTS = null;
  if (S.mapView === "photos") {
    try {
      await loadMapPoints();
    } catch {
      // The cache is already cleared, so a failed re-pull costs nothing: the
      // next switch to the photos view fetches again.
    }
  }
}
function renderMapViewNote() {
  const el = document.getElementById("map-view-note"); if (!el) return;
  const tiles = "Street map tiles are fetched online using coordinates only; your media stays on this computer.";
  if (S.mapView !== "photos") {
    el.innerHTML = `Nearby photos are grouped into places. ${tiles}`;
    return;
  }
  // One swatch per place would be a legend of hundreds; say what the colours
  // mean instead, and name the grey exception explicitly.
  const strays = MAP_POINTS_UNPLACED
    ? ` <span class="map-pointkey"><i></i>Grey: ${MAP_POINTS_UNPLACED.toLocaleString()} photo${MAP_POINTS_UNPLACED === 1 ? "" : "s"} that belong to no place.</span>`
    : "";
  el.innerHTML = `One dot per geotagged photo, coloured by the place it belongs to. ${tiles}${strays}`;
}
function updateMapStats() {
  const total = MAP_CLUSTERS.reduce((sum, cluster) => sum + cluster.count, 0);
  const named = MAP_CLUSTERS.filter(cluster => cluster.name && cluster.name.trim()).length;
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value.toLocaleString(); };
  set("map-photo-count", total); set("map-place-count", MAP_CLUSTERS.length); set("map-named-count", named);
  // The backend hides tiny one-off clusters (< 10 files, unless named/pinned) so
  // "Places" isn't dominated by single stray photos. Say so, only when it applies.
  const note = document.getElementById("map-hidden-note");
  if (note) {
    const hiddenPlaces = (MAP_HIDDEN && MAP_HIDDEN.places) || 0;
    if (hiddenPlaces > 0) {
      note.hidden = false;
      note.textContent = hiddenPlaces === 1
        ? "1 one-off spot with fewer than 10 photos isn’t shown as a place."
        : `${hiddenPlaces.toLocaleString()} one-off spots with fewer than 10 photos aren’t shown as places.`;
    } else {
      note.hidden = true;
      note.textContent = "";
    }
  }
}
function collageHTML(ids) {
  if (!ids.length) return `<div style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;font-size:20px">📍</div>`;
  const n = Math.min(ids.length, 4);
  // draggable=false: these sit inside merge-draggable place cards and would
  // otherwise hijack the card drag with their own image payload (faceCollage
  // does the same for person cards).
  const imgs = ids.slice(0, 4).map(id => `<img src="/thumb/${id}" loading="lazy" draggable="false" onerror="this.remove()">`).join("");
  return `<div class="cgrid n${n}">${imgs}</div>`;
}
function placeCollage(ids) {
  ids = (ids || []).filter(Boolean).slice(0, 4);
  if (!ids.length) return `<div class="placecollage"><div class="placeempty">📍</div></div>`;
  return `<div class="placecollage">${collageHTML(ids)}</div>`;
}
function renderPlaceGallery() {
  const wrap = document.getElementById("placegallery"); if (!wrap) return;
  if (!MAP_CLUSTERS.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<div class="place-gallery-head"><h3>Places</h3>
      <span class="muted">Named places first · then most photos</span></div>
    <div class="people" id="placegrid"></div>`;
  const grid = document.getElementById("placegrid");
  [...MAP_CLUSTERS].sort((a, b) => {
    const aUnnamed = !(a.name && a.name.trim()), bUnnamed = !(b.name && b.name.trim());
    return aUnnamed - bUnnamed || b.count - a.count || a.id - b.id;
  }).forEach(place => grid.appendChild(placeCard(place)));
}
function placeCard(place) {
  const card = document.createElement("div"); card.className = "pcard";
  card.onclick = guardCardClick(() => showPlaceFromGallery(place.id));
  const name = place.name ? esc(place.name) : "Name this place";
  card.innerHTML = placeCollage(place.thumb_ids) + `<div class="pmeta">
    <button class="pname ${place.name ? "" : "un"}" type="button">${name}</button>
    <div class="pcount">${place.count.toLocaleString()} photo${place.count === 1 ? "" : "s"}</div></div>`;
  card.querySelector(".pname").onclick = event => {
    event.stopPropagation();
    editPlaceCardName(card, place);
  };
  attachMergeDrag(card, { kind: "place", id: place.id, name: place.name, photos: place.count }, refreshPlacesAfterMerge);
  return card;
}
function showPlaceFromGallery(id) {
  const place = MAP_CLUSTERS.find(cluster => cluster.id === id);
  if (MAP && place) MAP.flyTo([place.lat, place.lon], Math.max(MAP.getZoom(), 14));
  selectPlaceCluster(id);
  document.getElementById("lmap")?.scrollIntoView({ behavior: "smooth", block: "center" });
}
function editPlaceCardName(card, place) {
  const meta = card.querySelector(".pmeta"); if (!meta) return;
  card.onclick = null;
  card.draggable = false;   // don't let an in-progress rename start a merge-drag
  meta.className = "pmeta pmeta-editing";
  meta.innerHTML = `<input value="${esc(place.name || "")}" placeholder="Place name" aria-label="Place name">
    <div class="pcount">${place.count.toLocaleString()} photo${place.count === 1 ? "" : "s"} · Enter or click away to save</div>`;
  const input = meta.querySelector("input");
  input.onclick = event => event.stopPropagation();
  let finished = false;
  input.addEventListener("blur", () => { if (!finished) { finished = true; savePlaceCardName(card, place, input); } });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    if (event.key === "Escape") { finished = true; card.replaceWith(placeCard(place)); }
  });
  input.focus(); input.select();
}
async function savePlaceCardName(card, place, input) {
  const name = input.value.trim();
  if (name === (place.name || "")) { card.replaceWith(placeCard(place)); return; }
  input.disabled = true;
  let result;
  try { result = await jpost("/api/map/cluster/rename", { cluster_id: place.id, name }); }
  catch (error) { result = { error: String(error) }; }
  if (!result || result.error) {
    toast("Couldn’t save the place name.", true); card.replaceWith(placeCard(place)); return;
  }
  place.name = name || null;
  await invalidateMapPoints();   // naming can promote a hidden spot to a real place
  updateMapStats();
  renderPlaceGallery();
  drawMap();
  if (S.mapSel === place.id) selectPlaceCluster(place.id);
}
const PLACE_PAGE_SIZE = 120;
async function selectPlaceCluster(id) {
  S.mapSel = id;
  const side = document.getElementById("mapside");
  const wrap = side && side.closest(".mapwrap");
  if (wrap) wrap.classList.add("has-selection");
  setTimeout(() => { if (MAP) { MAP.invalidateSize(); drawMap(); } }, 0);
  side.innerHTML = `<div class="muted">Loading…</div>`;
  // root was missing here before -- the endpoint requires it and silently
  // failed the request without it (caught, swallowed, no response sent).
  const c = await jget(`/api/map/cluster/${id}?root=${S.arch.id}&limit=${PLACE_PAGE_SIZE}`);
  if (S.mapSel !== id) return; // superseded by a newer click
  if (!c || c.error) { side.innerHTML = `<div class="muted">Place not found.</div>`; return; }
  const safeName = (c.name || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, "&quot;");
  const displayName = c.name ? esc(c.name) : "Name this place";
  side.innerHTML = `
    <div class="mapside-name" id="mapsidename">
      <div class="mapside-title"><button class="person-name-button ${c.name ? "" : "un"}" onclick="editClusterName(${id},'${safeName}')">${displayName}</button>
        <span class="muted">${c.total.toLocaleString()} item${c.total === 1 ? "" : "s"}</span></div>
      <div class="mapside-actions"><button class="close-side" onclick="closePlaceCluster()" aria-label="Close place">×</button></div>
    </div>
    ${mergesPanel(c.merges, "place")}
    <div class="grid" id="mapsidegrid" style="grid-template-columns:repeat(auto-fill,minmax(80px,1fr))"></div>
    <div class="infinite-status" id="mapside-sentinel" aria-live="polite"></div>`;
  let firstPage = c.members;
  startInfiniteList("placeList", {
    sentinelId: "mapside-sentinel", pageSize: PLACE_PAGE_SIZE, root: side,
    fetchPage: async offset => {
      if (firstPage) { const page = firstPage; firstPage = null; return page; }
      const res = await jget(`/api/map/cluster/${id}?root=${S.arch.id}&offset=${offset}&limit=${PLACE_PAGE_SIZE}`);
      return (res && res.members) || [];
    },
    onPage: (items, { first }) => {
      const grid = document.getElementById("mapsidegrid");
      if (first) grid.replaceChildren();
      items.forEach(it => grid.appendChild(tile(it)));
    },
  });
}
function editClusterName(id, current) {
  const box = document.getElementById("mapsidename");
  box.innerHTML = `<div class="inline-name-editor"><input id="mapsidenameinput" value="${esc(current)}" placeholder="e.g. Grandma’s house" aria-label="Place name"></div>`;
  const input = document.getElementById("mapsidenameinput"); input.focus(); input.select();
  let finished = false;
  input.addEventListener("blur", () => { if (!finished) { finished = true; saveClusterName(id, input); } });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { finished = true; selectPlaceCluster(id); }
  });
}
async function saveClusterName(id, input) {
  const name = input.value.trim(); input.disabled = true;
  const result = await jpost("/api/map/cluster/rename", { cluster_id: id, name });
  if (!result || result.error) { toast("Couldn’t save the place name.", true); selectPlaceCluster(id); return; }
  const mc = MAP_CLUSTERS.find(c => c.id === id); if (mc) mc.name = name || null;
  await invalidateMapPoints();   // naming can promote a hidden spot to a real place
  updateMapStats();
  renderPlaceGallery();
  drawMap();
  selectPlaceCluster(id);
}
function closePlaceCluster() {
  S.mapSel = null;
  const wrap = document.querySelector(".mapwrap"); if (wrap) wrap.classList.remove("has-selection");
  const side = document.getElementById("mapside"); if (side) side.innerHTML = "";
  setTimeout(() => { if (MAP) { MAP.invalidateSize(); drawMap(); } }, 0);
}
// Re-pull the whole cluster list after a place merge (or its undo) rather than
// patching MAP_CLUSTERS in place: a merge can change member_count, the survivor's
// centroid (weighted mean, unless pinned), and which clusters exist at all, and
// the /api/map/clusters?root= floor on "hidden one-off spots" (map-hidden-note)
// needs to be recomputed the same way too. `survivor` is the merged place object
// the backend returned ({id, name, count}); pass it so the side panel can follow
// the merge to wherever the dragged/dropped place actually landed -- the backend
// picks the surviving id by its own named/pinned/count/id chain, which does not
// always match the drop TARGET card runMerge sent as `a`.
async function refreshPlacesAfterMerge(survivor) {
  const { clusters, hidden } = await jget("/api/map/clusters?root=" + S.arch.id);
  MAP_CLUSTERS = clusters;
  MAP_HIDDEN = hidden || {};
  await invalidateMapPoints();
  updateMapStats();
  renderPlaceGallery();
  drawMap();
  renderMapViewNote();
  if (S.mapSel == null) return;
  const stillOpen = MAP_CLUSTERS.some(c => c.id === S.mapSel);
  if (stillOpen) selectPlaceCluster(S.mapSel);
  else if (survivor) selectPlaceCluster(survivor.id);   // the open place was absorbed -- follow it
}
function drawMap() {
  if (!MAP) return;
  if (MAP_LAYER) { MAP.removeLayer(MAP_LAYER); MAP_LAYER = null; }
  if (S.mapView === "photos" && MAP_POINTS) { showPhotoPoints(); return; }
  hidePhotoPoints();
  MAP_LAYER = L.layerGroup().addTo(MAP);
  const R = 54; // px screen-bucket radius -- groups nearby PLACES at low zoom
  const bounds = MAP.getBounds(), buckets = {};
  MAP_CLUSTERS.forEach(c => {
    if (!bounds.contains([c.lat, c.lon])) return;
    const pt = MAP.latLngToContainerPoint([c.lat, c.lon]);
    const key = Math.round(pt.x / R) + "_" + Math.round(pt.y / R);
    (buckets[key] = buckets[key] || []).push(c);
  });
  Object.values(buckets).forEach(grp => {
    const lat = grp.reduce((a, c) => a + c.lat, 0) / grp.length;
    const lon = grp.reduce((a, c) => a + c.lon, 0) / grp.length;
    if (grp.length === 1) {
      const c = grp[0];
      const badge = c.count > c.thumb_ids.length ? `<span class="mk-badge">${c.count > 999 ? "999+" : c.count}</span>` : "";
      const icon = L.divIcon({
        className: "", iconSize: [46, 46], html:
          `<div class="mk${c.id === S.mapSel ? " mk-sel" : ""}">${collageHTML(c.thumb_ids)}${badge}</div>`
      });
      const mk = L.marker([lat, lon], { icon }).addTo(MAP_LAYER).on("click", () => selectPlaceCluster(c.id));
      mk.bindTooltip(c.name || "Name this place", { direction: "top", offset: [0, -24], opacity: 0.95 });
    } else {
      const total = grp.reduce((a, c) => a + c.count, 0);
      const rep = grp[0].thumb_ids[0];
      const icon = L.divIcon({
        className: "", iconSize: [52, 52], html:
          `<div class="mk mk-cluster">${rep ? `<img src="/thumb/${rep}" onerror="this.remove()">` : ""}<span>${total.toLocaleString()}</span></div>`
      });
      L.marker([lat, lon], { icon }).addTo(MAP_LAYER).on("click", () => {
        MAP.flyToBounds(L.latLngBounds(grp.map(c => [c.lat, c.lon])), { padding: [60, 60], maxZoom: 16 });
      });
    }
  });
}

// One hue per place, from its id: no palette to run out of, and a place
// keeps its colour between redraws and sessions. The golden-angle step is
// what keeps consecutive ids visibly different instead of a slow gradient.
function pointColour(clusterId) {
  if (!clusterId) return "#98a1ae";      // belongs to no shown place
  return `hsl(${(clusterId * 137.508) % 360}, 68%, 55%)`;
}
// Every photo as its own translucent dot, so the true spread of a place is
// visible (and overlapping shots pile up into a denser blob). Drawn on ONE
// canvas rather than as DOM markers: tens of thousands of divs would lock
// the page up.
//
// Unlike the clustered path this does NOT rebuild on every pan. Building
// ~16k Leaflet layers costs ~0.5s, while the canvas renderer redraws the
// same 16k circles on a move in ~80ms all by itself -- so the layer is
// built once and only thrown away when something it actually depends on
// changes (the points, the open place, or the zoom-derived dot size).
function showPhotoPoints() {
  const zoom = MAP.getZoom();
  // A little bigger as you zoom in: dense at world view (where a city is a
  // few pixels), individually clickable once you're over a street. Bucketed
  // so ordinary zooming doesn't trigger a rebuild.
  const radius = zoom >= 15 ? 6 : zoom >= 11 ? 4.5 : 3.5;
  // With a place open, its photos stay solid and the rest recede, so
  // clicking a place card means the same thing in both views.
  const sel = S.mapSel || 0;
  const built = MAP_POINT_BUILT;
  if (MAP_POINT_LAYER && built && built.points === MAP_POINTS
      && built.sel === sel && built.radius === radius) {
    if (!MAP.hasLayer(MAP_POINT_LAYER)) MAP_POINT_LAYER.addTo(MAP);
    return;
  }
  hidePhotoPoints();
  if (!MAP_POINT_CANVAS) MAP_POINT_CANVAS = L.canvas({ padding: 0.2 });
  const layer = L.layerGroup();
  MAP_POINTS.forEach(([lat, lon, cid, fileId]) => {
    L.circleMarker([lat, lon], {
      renderer: MAP_POINT_CANVAS, radius, weight: 0,
      fillColor: pointColour(cid),
      fillOpacity: !sel ? 0.55 : (cid === sel ? 0.85 : 0.15),
    }).addTo(layer).on("click", () => openItem(fileId));
  });
  layer.addTo(MAP);
  MAP_POINT_LAYER = layer;
  MAP_POINT_BUILT = { points: MAP_POINTS, sel, radius };
}
function hidePhotoPoints() {
  if (MAP_POINT_LAYER && MAP) MAP.removeLayer(MAP_POINT_LAYER);
  MAP_POINT_LAYER = null;
  MAP_POINT_BUILT = null;
}

/* ---------- browse ---------- */
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
const GRID_PAGE_SIZE = 120, GRID_MAX_PAGES = 4;
async function renderPhotos(m) {
  const gen = NAV;
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
  if (gen !== NAV) return;
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
  const gen = NAV;
  const f = await jget("/api/browse/filters?root=" + S.arch.id);
  if (gen !== NAV) return;
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
function selVal(id) { const e = document.getElementById(id); return e ? e.value : ""; }
function peopleFilterHTML(prefix, people) {
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
function checkedPeople(prefix) {
  return [...document.querySelectorAll(`#${prefix}-people-filter input:checked`)].map(e => e.value);
}
function clearPeopleChecks(prefix) {
  document.querySelectorAll(`#${prefix}-people-filter input:checked`).forEach(e => e.checked = false);
}
function updatePeopleFilterLabel(prefix, people) {
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
function tile(it, resultIndex = null) {
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
function personTile(it, personId) {
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

async function renderFaces(m) {
  const gen = NAV, root = S.arch.id;
  S.facePerson = null;
  const sum = await jget("/api/faces/summary?root=" + root);
  if (gen !== NAV) return;
  if (!sum.backend_available) {
    m.innerHTML = `<div class="pagehead"><div><h2 class="sec">People</h2><p>Find familiar faces and organize them with names.</p></div></div>
      <div class="panel"><div class="d pending"><span class="dot pending"></span>Face detection needs OpenCV's DNN face module.</div>
      <p class="muted">Install a modern <code>opencv-python</code> (the <code>media</code> extra) and reopen this tab.</p></div>`;
    return;
  }
  S.faceSum = sum;
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">People</h2><p>Find familiar faces, review matches, and add names without leaving this page.</p></div></div>
    <div class="statrow">
      <div class="stat"><div class="k">People</div><div class="v" id="fs-people">${sum.people.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Faces</div><div class="v" id="fs-faces">${sum.faces.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Photos with faces</div><div class="v" id="fs-photos">${sum.photos_with_faces.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Scanned</div><div class="v" id="fs-scanned">${sum.scanned.toLocaleString()} <small>/ ${sum.total_images.toLocaleString()}</small></div></div>
    </div>
    <div class="panel" id="facejob"></div>
    <div id="peoplewrap"><div class="muted" style="padding:20px">Loading people…</div></div>`;
  renderFaceControls();
  renderPeople();
  startFacePoll();   // reflects a face job's progress and refreshes when it ends
}
// Faces run automatically as part of the background pipeline (scan → dates →
// faces → duplicates); this panel only reports status. Clustering into people
// re-runs automatically after every detection chunk, so there is no manual
// start/stop/recompute; it all happens on its own and halts only on app close.
// One-line status, same shape as the Pets panel (#petjob): no progress bar,
// no emoji, exactly one row so the panel never reserves empty space. People
// and Pets both report on the same fused backend `detect` stage, so they
// share this wording via detectStatusRow (the sidebar chip owns "running").
function renderFaceControls(failed) {
  const el = document.getElementById("facejob"); if (!el) return;
  el.innerHTML = detectStatusRow(S.faceSum, failed);
}
// Shared status line for the fused people+pets `detect` stage. `failed`
// is the People-only retry-cooldown message; Pets currently always passes
// null. First-run addendum applies to both since it's the same model.
function detectStatusRow(sum, failed) {
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

function startFacePoll() { stopPoll(); S.poll = setInterval(faceTick, 1500); faceTick(); }
// Live refresh while a faces job runs: the stat tiles tick every poll, and
// the people grid is *patched* (syncPeopleGrid) rather than rebuilt, so the
// page never resets under the user -- scroll position, the pages the
// infinite list has already loaded and the "Same person?" review queue all
// survive, and only cards whose data actually changed are touched.
async function faceTick() {
  const area = document.getElementById("facejob"); if (!area) { stopPoll(); return; }
  const [snap, sum] = await Promise.all([
    jget("/api/pipeline?root=" + S.arch.id),
    jget("/api/faces/summary?root=" + S.arch.id)]);
  const facesStage = (snap.stages || []).find(s => s.id === "detect");
  const fj = facesStage && facesStage.state === "running" ? facesStage.progress : null;
  // Keep a failed attempt visible during the scheduler's retry cooldown
  // instead of making the progress panel blink.
  const failedFace = facesStage && facesStage.state === "error" ? facesStage : null;
  const wasRunning = S.faceJobRunning; S.faceJobRunning = !!fj;
  const prev = S.faceSum || {}; S.faceSum = sum;
  setText("fs-people", sum.people.toLocaleString());
  setText("fs-faces", sum.faces.toLocaleString());
  setText("fs-photos", sum.photos_with_faces.toLocaleString());
  const sc = document.getElementById("fs-scanned");
  if (sc) sc.innerHTML = `${sum.scanned.toLocaleString()} <small>/ ${sum.total_images.toLocaleString()}</small>`;
  const failed = failedFace && sum.unscanned > 0
    ? (failedFace.message || "The face worker stopped before reporting progress.") : null;
  renderFaceControls(failed);
  if (fj) {
    if (sum.people !== prev.people || sum.faces !== prev.faces) syncPeopleGrid();
  } else if (wasRunning) {
    syncPeopleGrid();   // final pass finished → reconcile once more
  }
}
const PEOPLE_PAGE_SIZE = 120;
async function fetchPeoplePage(offset) {
  const res = await jget(`/api/faces/persons?root=${S.arch.id}&offset=${offset}&limit=${PEOPLE_PAGE_SIZE}`);
  return res.people;
}
async function renderPeople() {
  const wrap = document.getElementById("peoplewrap"); if (!wrap) return;
  wrap.innerHTML = `<div id="suggestwrap"></div><div class="people" id="peoplegrid"></div>
    <div class="infinite-status" id="people-sentinel" aria-live="polite"></div>`;
  startInfiniteList("peopleList", {
    sentinelId: "people-sentinel", pageSize: PEOPLE_PAGE_SIZE,
    fetchPage: fetchPeoplePage,
    onPage: (people, { first, done }) => {
      if (first && done && !people.length) {
        const s = S.faceSum;
        wrap.innerHTML = `<div class="muted" style="padding:20px">` + (
          s.faces > 0
            ? `No recurring people found yet. ${s.faces.toLocaleString()} face${s.faces === 1 ? "" : "s"} detected, but none repeat often enough to group into a person. People appear automatically as more photos are scanned.`
            : (s.scanned > 0
              ? `No faces detected in the scanned photos.`
              : `No faces yet; detection runs automatically in the background.`)) + `</div>`;
        return;
      }
      const grid = document.getElementById("peoplegrid");
      if (first) { grid.innerHTML = ""; loadSuggestions(); }
      people.forEach(p => grid.appendChild(personCard(p)));
    },
  });
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
function syncCardGrid(grid, items, { keyOf, make, update, complete, empty }) {
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
/* Patch the people grid to match the server. The server clamps the endpoint
   to 500, so a grid scrolled past that syncs only its first 500 cards; the
   rest stay as they were until the next full renderPeople(). */
const PEOPLE_SYNC_LIMIT = 500;
async function syncPeopleGrid() {
  if (S.section !== "people" || S.facePerson != null) return;
  if (S.peopleSyncing) return;                       // one in flight at a time
  // Empty state (no #peoplegrid, just the "no people yet" message): the
  // first cluster to land needs the full render to build the grid at all.
  if (!document.getElementById("peoplegrid")) {
    if (S.faceSum && S.faceSum.people > 0) renderPeople();
    return;
  }
  const st = S.peopleList; if (!st) return;
  const limit = Math.min(PEOPLE_SYNC_LIMIT, Math.max(PEOPLE_PAGE_SIZE, st.offset));
  S.peopleSyncing = true;
  let people;
  try { people = (await jget(`/api/faces/persons?root=${S.arch.id}&offset=0&limit=${limit}`)).people; }
  catch (e) { return; }
  finally { S.peopleSyncing = false; }
  const grid = document.getElementById("peoplegrid");
  if (!grid || S.peopleList !== st) return;          // navigated away mid-fetch
  const complete = people.length < limit;
  // Keep the infinite list's cursor equal to what's on screen, so the next
  // page picks up right after the last card however many were added/pruned.
  st.offset = syncCardGrid(grid, people, {
    keyOf: p => p.id, make: personCard, update: updatePersonCard, complete });
  if (complete) st.done = (st.offset === people.length);
  // A review queue the user has worked through can be refilled silently;
  // one still in progress is never disturbed. Keyed on the people count so
  // this fires per clustering pass, not on every 1.5s poll.
  const stamp = S.faceSum ? S.faceSum.people : 0;
  const q = S.suggest;
  if ((!q || q.idx >= q.list.length) && S.suggestStamp !== stamp) loadSuggestions();
}
async function loadSuggestions() {
  S.suggestStamp = S.faceSum ? S.faceSum.people : 0;
  const sug = await jget("/api/faces/suggestions?root=" + S.arch.id + "&limit=60").catch(() => null);
  S.suggest = { list: (sug && sug.suggestions) || [], idx: 0, total: (sug && sug.total) || 0 };
  renderSuggest();
}
function renderSuggest() {
  const w = document.getElementById("suggestwrap"); if (!w) return;
  const st = S.suggest || { list: [], idx: 0, total: 0 }, s = st.list[st.idx];
  if (!s) { w.innerHTML = ""; return; }
  const left = Math.max(1, (st.total || st.list.length) - st.idx);
  const face = o => `<div class="sug-face">
      ${faceCollage(o.faces_preview && o.faces_preview.length ? o.faces_preview : [o.cover_face_id])}
      <div class="sug-lbl">${o.name ? esc(o.name) : "Name this person"}</div>
      <div class="sug-cnt">${o.faces.toLocaleString()} face${o.faces === 1 ? '' : 's'}</div></div>`;
  w.innerHTML = `<div class="suggest">
    <div class="sug-head">Same person? <span class="muted">· ${left.toLocaleString()} to review · ${Math.round(s.sim * 100)}% match</span></div>
    <div class="sug-pair">${face(s.a)}<div class="sug-q">≟</div>${face(s.b)}</div>
    <div class="sug-btns">
      <button class="sug-yes" onclick="answerSuggest('same')">Same person</button>
      <button class="sug-no" onclick="answerSuggest('different')">Not the same</button>
      <button class="sug-skip" onclick="answerSuggest('skip')">Skip</button>
    </div>
    <div class="sug-extra"><button onclick="answerSuggest('notpeople')">🚫 Neither is a person; hide both (dolls / pets / cartoons)</button></div>
    </div>`;
}
async function answerSuggest(kind) {
  const st = S.suggest, s = st.list && st.list[st.idx]; if (!s) return;
  // drop any later queued pair that references a now-removed cluster
  const dropRefs = ids => { st.list = st.list.filter((x, ix) => ix <= st.idx || (!ids.includes(x.a.id) && !ids.includes(x.b.id))); };
  if (kind === 'same') {
    const res = await jpost('/api/faces/merge', { a: s.a.id, b: s.b.id });
    if (res && res.error) { alert(res.error); return; }
    if (res && res.person) { const kept = res.person.id, dropped = (s.a.id === kept ? s.b.id : s.a.id); dropRefs([dropped]); renderPeopleGrid(); }
  } else if (kind === 'different') {
    await jpost('/api/faces/different', { a: s.a.id, b: s.b.id });
  } else if (kind === 'skip') {
    await jpost('/api/faces/skip', { a: s.a.id, b: s.b.id });
  } else if (kind === 'notpeople') {
    const reason = chooseNonhumanKind(); if (!reason) return;
    await jpost('/api/faces/hide', { person_id: s.a.id, kind: reason });
    await jpost('/api/faces/hide', { person_id: s.b.id, kind: reason });
    dropRefs([s.a.id, s.b.id]); renderPeopleGrid();
  }
  if (st.total > 0) st.total--;
  st.idx++;
  if (st.idx >= st.list.length) { renderPeople(); return; }  // reload grid + fresh queue
  renderSuggest();
}
async function renderPeopleGrid() {
  if (!document.getElementById("peoplegrid")) return;
  startInfiniteList("peopleList", {
    sentinelId: "people-sentinel", pageSize: PEOPLE_PAGE_SIZE,
    fetchPage: fetchPeoplePage,
    onPage: (people, { first }) => {
      const grid = document.getElementById("peoplegrid");
      if (first) grid.innerHTML = "";
      people.forEach(p => grid.appendChild(personCard(p)));
    },
  });
}
async function hidePerson(id) {
  if (!confirm('Not a person? Its faces get marked as a doll/animal/cartoon and are left out of clustering. It disappears from People.')) return;
  const kind = chooseNonhumanKind(); if (!kind) return;
  await jpost('/api/faces/hide', { person_id: id, kind });
  backToPeople();
}
function chooseNonhumanKind() {
  const answer = prompt("Classification: animal, toy, cartoon, or false_detection", "false_detection");
  if (answer === null) return null;
  const value = answer.trim().toLowerCase().replace(/\s+/g, "_");
  if (!["animal", "toy", "cartoon", "false_detection"].includes(value)) {
    toast("Use animal, toy, cartoon, or false_detection.", true); return null;
  }
  return value;
}
/* ---------- drag-to-merge (People, Pets & Places grids) ----------
   Dragging one group card onto another folds the dragged ("source") group
   into the one it was dropped on ("target") -- the drop target surviving
   matches the "drop THIS onto THAT" mental model. Place cards share the
   same .pcard styling and the same drag machinery below (attachMergeDrag
   is called from placeCard too) -- geo/clusters.py's 300m grid-union
   routinely splits one real location into adjacent clusters, and this is
   how a person folds them back into one. */
const MERGE_LABELS = { pet: "Merge pets", person: "Merge people", place: "Merge places" };
let DRAG_MERGE = null;        // { kind, id, name, photos } of the card mid-drag
let MERGE_DROP_GUARD = false; // true for one tick after a drop/dragend, so the
// native click that a completed drag can still fire on the source card
// (Safari has done this) doesn't also open the person/pet it came from.
function guardCardClick(fn) {
  return (...args) => { if (!MERGE_DROP_GUARD) fn(...args); };
}
function attachMergeDrag(card, info, onMerged) {
  card.draggable = true;
  card.dataset.mergeId = String(info.id);
  // Fed to the .drop-target::after pill via CSS attr() -- see its rule.
  card.dataset.mergeLabel = MERGE_LABELS[info.kind] || "Merge";
  let depth = 0;   // nested dragenter/dragleave counter -- entering a child
  // element (thumbnail, name button) fires enter/leave on it too, which
  // would flicker the ring off and on if we tracked plain booleans instead.
  const release = () => {
    card.classList.remove("dragging"); DRAG_MERGE = null;
    // A drag abandoned with Esc while hovering a target fires no dragleave
    // there, so sweep every ring rather than trusting each card to clear its
    // own. (Their counters self-heal on the next dragenter, below.)
    document.querySelectorAll(".pcard.drop-target")
      .forEach(el => el.classList.remove("drop-target"));
    MERGE_DROP_GUARD = true; setTimeout(() => { MERGE_DROP_GUARD = false; }, 0);
  };
  const isValidTarget = () => DRAG_MERGE && DRAG_MERGE.kind === info.kind && DRAG_MERGE.id !== info.id;
  card.addEventListener("dragstart", e => {
    if (!card.draggable) { e.preventDefault(); return; }
    DRAG_MERGE = info;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(info.id));
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", release);
  card.addEventListener("dragenter", () => {
    if (!isValidTarget()) return;
    if (!card.classList.contains("drop-target")) depth = 0;   // self-heal a counter
    depth++; card.classList.add("drop-target");               // left stale by a swept ring
  });
  card.addEventListener("dragleave", () => { if (depth > 0 && --depth === 0) card.classList.remove("drop-target"); });
  card.addEventListener("dragover", e => { if (isValidTarget()) e.preventDefault(); });   // preventDefault = allow the drop
  card.addEventListener("drop", async e => {
    e.preventDefault();
    depth = 0; card.classList.remove("drop-target");
    const source = DRAG_MERGE;
    release();
    if (!source || source.kind !== info.kind || source.id === info.id) return;  // same card, or a stale/foreign drag
    await runMerge(source, info, onMerged);
  });
}
async function runMerge(source, target, onMerged) {
  const sourceName = (source.name || "").trim(), targetName = (target.name || "").trim();
  const noun = target.kind === "pet" ? "pets" : target.kind === "place" ? "places" : "people";
  const title = `Merge these two ${noun}?`;
  let body, options, preselect;
  if (sourceName && targetName && sourceName !== targetName) {
    body = "They have different names. Which one should stay?";
    preselect = targetName;   // the drop target is the one that stays
    options = [
      { value: targetName, label: targetName, count: target.photos },
      { value: sourceName, label: sourceName, count: source.photos },
    ];
  } else if (sourceName || targetName) {
    // Exactly one side is named (or both share the same name): the
    // backend always keeps the named side, so say so concretely rather
    // than asking a question that has only one possible answer.
    const keptName = targetName || sourceName;
    const droppedPhotos = targetName ? source.photos : target.photos;
    body = `“${esc(keptName)}” will absorb the other group’s ${droppedPhotos.toLocaleString()} `
      + `photo${droppedPhotos === 1 ? "" : "s"}.`;
    preselect = keptName;
    options = [];
  } else {
    body = `The dragged group’s ${source.photos.toLocaleString()} `
      + `photo${source.photos === 1 ? "" : "s"} will be folded in.`;
    preselect = "";
    options = [];
  }
  // Places-only: a merge here can fold in a group that's nowhere near the
  // survivor (two clusters that only look close on the map at low zoom),
  // so ask the backend how wide the merged spread would actually be and
  // surface it as a caution -- never as a block. Preview is advisory UX,
  // not a gate: any failure (network, 4xx, a response with no `warn`)
  // just means no warning shown, and the merge proceeds exactly as it
  // would have with no preview at all.
  let warning;
  if (target.kind === "place") {
    const preview = await jget(`/api/map/cluster/merge-preview?root=${S.arch.id}&a=${target.id}&b=${source.id}`)
      .catch(() => null);
    if (preview && !preview.error && preview.warn) {
      // Sub-10km spans keep one decimal (the difference between 3km and
      // 9km still matters at that scale); above that, round to a whole
      // number like every other distance/count in this dialog.
      const dist = preview.span_km < 10 ? preview.span_km.toFixed(1) : Math.round(preview.span_km).toLocaleString();
      warning = `These photos span a wide area — some sit ${dist} km from the centre of the merged place.`;
    }
  }
  const name = await askMergeName({ title, body, options, preselect, warning });
  if (name === null) return;   // cancelled: no change
  const url = target.kind === "pet" ? "/api/pets/merge"
    : target.kind === "place" ? "/api/map/cluster/merge" : "/api/faces/merge";
  const reqBody = { a: target.id, b: source.id };
  if (name) reqBody.name = name;
  const res = await jpost(url, reqBody);
  if (res && res.error) { toast(res.error, true); return; }
  const merged = target.kind === "pet" ? res.pet : target.kind === "place" ? res.place : res.person;
  const count = (merged && (target.kind === "pet" ? merged.detections
    : target.kind === "place" ? merged.count : merged.face_count)) || 0;
  toast(merged && merged.name ? `Merged · “${merged.name}” now has ${count.toLocaleString()} photos`
    : `Merged · ${count.toLocaleString()} photos`);
  onMerged(merged);   // places' onMerged (refreshPlacesAfterMerge) uses this to follow the survivor; others ignore it
}
// Small centered confirm dialog for the both-named drag-merge case. Resolves
// to the chosen name, or null if the user backed out via Cancel/Esc/backdrop.
// Separate markup from #modal (the media viewer) -- unrelated concerns.
let _mergeAskResolve = null;
function mergeAskCancel() { if (_mergeAskResolve) _mergeAskResolve(null); }
function askMergeName({ title, body, options, preselect, warning }) {
  return new Promise(resolve => {
    const backdrop = document.getElementById("mergeask-backdrop");
    const dlg = document.getElementById("mergeask");
    const optsEl = document.getElementById("mergeask-options");
    const mergeBtn = document.getElementById("mergeask-merge");
    const cancelBtn = document.getElementById("mergeask-cancel");
    const previouslyFocused = document.activeElement;
    document.getElementById("mergeask-title").textContent = title;
    document.getElementById("mergeask-body").textContent = body;
    // Places-only wide-area caution (runMerge builds this string from the
    // merge-preview endpoint). The dialog element is reused for every
    // drag-merge -- People/Pets never pass `warning` -- so this must be
    // reset on every call, or a stale Places warning would linger onto
    // the next, unrelated merge.
    const warnEl = document.getElementById("mergeask-warning");
    // The leading "⚠ " is a visual cue only -- the sentence itself
    // (built in runMerge) carries the actual meaning for anyone/anything
    // that can't render or announce the glyph.
    warnEl.textContent = warning ? `⚠ ${warning}` : "";
    warnEl.hidden = !warning;
    const opts = options || [];
    // No radios for the (now much more common) case where there's
    // nothing to choose between -- confirming every drag-merge (not just
    // the both-named one) means most calls pass an empty options list.
    optsEl.style.display = opts.length ? "" : "none";
    optsEl.innerHTML = opts.map(o => `<label class="mergeask-opt">
        <input type="radio" name="mergeask-name" value="${esc(o.value)}" ${o.value === preselect ? "checked" : ""}>
        <span>${esc(o.label)}</span><span class="muted">${o.count.toLocaleString()} photo${o.count === 1 ? "" : "s"}</span>
      </label>`).join("");
    // With no radios there's nothing to read from the DOM: resolve
    // straight to preselect (the single candidate name, or "" when
    // neither side is named) rather than the old fallback chain, which
    // only worked by accident (querySelector-on-empty happened to hit
    // its `|| {}` branch).
    const chosen = () => {
      if (!opts.length) return preselect || "";
      const checked = optsEl.querySelector("input[name=mergeask-name]:checked");
      return checked ? checked.value : (preselect || "");
    };
    const onKey = e => {
      // Bound to the document, not the dialog: a keydown only reaches the
      // dialog while focus is inside it, and focus can legitimately sit
      // elsewhere (a backdrop click, a re-render). Capture + stopPropagation
      // so the page's global Escape handler (#modal / the settings drawer)
      // doesn't also react to the same keypress.
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(null); }
      else if (e.key === "Enter" && document.activeElement !== cancelBtn) { e.preventDefault(); e.stopPropagation(); finish(chosen()); }
    };
    function finish(value) {
      backdrop.classList.remove("open"); dlg.classList.remove("open");
      document.removeEventListener("keydown", onKey, true);
      mergeBtn.onclick = cancelBtn.onclick = null;
      _mergeAskResolve = null;
      if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
      resolve(value);
    }
    _mergeAskResolve = finish;
    mergeBtn.onclick = () => finish(chosen());
    cancelBtn.onclick = () => finish(null);
    backdrop.classList.add("open"); dlg.classList.add("open");
    document.addEventListener("keydown", onKey, true);
    // Flush the style change before focusing: the dialog is visibility:hidden
    // until .open lands, and an element the browser still considers hidden
    // silently refuses focus (measured: activeElement stayed on <body>).
    void dlg.offsetWidth;
    // Empty options -> nothing to check -> focus lands on Merge directly.
    (opts.length && optsEl.querySelector("input[name=mergeask-name]:checked") || mergeBtn).focus();
  });
}
// Undo-a-merge panel: one row per drag-merge folded into this person/pet,
// with an Undo button. `kind` picks the unmerge endpoint and how to
// refresh afterwards. Rendered on the detail page only (not the grid
// cards) -- merges is whatever face_person/pet_group return.
function mergesPanel(merges, kind) {
  if (!merges || !merges.length) return "";
  const rows = merges.map(m => {
    const label = m.dropped_name ? `“${esc(m.dropped_name)}”` : "an unnamed group";
    const n = m.photos_folded_in;
    return `<div class="merge-row">
        <span>Merged in ${label} · ${n.toLocaleString()} photo${n === 1 ? "" : "s"}</span>
        <button class="linkbtn" type="button" onclick="undoMerge(${m.id},'${kind}')">Undo</button>
      </div>`;
  }).join("");
  return `<div class="panel merges-panel"><h3>Merges</h3>${rows}</div>`;
}
async function undoMerge(mergeId, kind) {
  const url = kind === "pet" ? "/api/pets/unmerge"
    : kind === "place" ? "/api/map/cluster/unmerge" : "/api/faces/unmerge";
  const res = await jpost(url, { merge_id: mergeId });
  if (!res || res.error) { toast((res && res.error) || "Couldn’t undo that merge.", true); return; }
  // People/pets requeue a background recluster, so their toast says so; a place
  // merge is a direct row move/restore (places.py's unmerge_place_clusters),
  // nothing gets queued, so "Undone" alone is accurate here.
  toast(kind === "place" ? "Undone" : "Undone — regrouping in the background…");
  if (kind === "pet") { if (S.currentPet) showPet(S.currentPet.id); }
  else if (kind === "place") { refreshPlacesAfterMerge(); }
  else if (S.facePerson != null) showPerson(S.facePerson);
}
// Up to 4 faces as a 2x2 collage (a single face fills the square). `ids` is the
// person's faces_preview, with cover_face_id as fallback for old payloads.
function faceCollage(ids) {
  ids = (ids || []).filter(Boolean).slice(0, 4);
  if (ids.length <= 1) {
    const id = ids[0];
    // draggable=false: these images sit inside merge-draggable person cards
    // and would otherwise hijack the card drag with their own payload.
    return id ? `<img class="face" src="/faceThumb/${id}" loading="lazy" draggable="false" onerror="this.style.visibility='hidden'">`
      : `<div class="face"></div>`;
  }
  let cells = "";
  for (let i = 0; i < 4; i++) cells += ids[i]
    ? `<img src="/faceThumb/${ids[i]}" loading="lazy" draggable="false" onerror="this.style.visibility='hidden'">`
    : `<div class="cempty"></div>`;
  return `<div class="facecollage">${cells}</div>`;
}
// The preview face ids a card's collage is built from, as a string, so
// syncPeopleGrid can tell "same faces, new count" from "new faces".
function personCoverIds(p) {
  return (p.faces_preview && p.faces_preview.length ? p.faces_preview : [p.cover_face_id])
    .filter(Boolean).slice(0, 4);
}
function personMetaInner(p) {
  const nm = p.name ? esc(p.name) : "Name this person";
  return `<button class="pname ${p.name ? "" : "un"}" type="button">${nm}</button>
    <div class="pcount">${p.photos.toLocaleString()} photo${p.photos === 1 ? "" : "s"}</div>`;
}
function personCard(p) {
  const d = document.createElement("div"); d.className = "pcard"; d.onclick = guardCardClick(() => showPerson(p.id));
  d.dataset.syncKey = String(p.id);
  // Mutable so syncPeopleGrid can refresh a renamed/re-counted person without
  // re-running attachMergeDrag (which would stack a second set of listeners).
  d._merge = { kind: "person", id: p.id, name: p.name, photos: p.photos };
  d.dataset.cover = personCoverIds(p).join(",");
  d.innerHTML = faceCollage(personCoverIds(p)) + `<div class="pmeta">${personMetaInner(p)}</div>`;
  d.querySelector(".pname").onclick = e => { e.stopPropagation(); editPersonCardName(d, p); };
  attachMergeDrag(d, d._merge, renderPeopleGrid);
  return d;
}
// In-place refresh of one already-rendered card. Only the parts that actually
// changed are touched -- an unchanged collage keeps its <img> nodes, so a
// person whose photo count ticked up doesn't reload (and visibly blink) its
// thumbnails. Returns false if the card is mid-rename and must be left alone.
function updatePersonCard(card, p) {
  const meta = card.querySelector(".pmeta");
  if (!meta || meta.classList.contains("pmeta-editing")) return false;
  const cover = personCoverIds(p).join(",");
  if (card.dataset.cover !== cover) {
    card.dataset.cover = cover;
    card.firstElementChild.outerHTML = faceCollage(personCoverIds(p));
  }
  meta.innerHTML = personMetaInner(p);
  meta.querySelector(".pname").onclick = e => { e.stopPropagation(); editPersonCardName(card, p); };
  Object.assign(card._merge, { name: p.name, photos: p.photos });
  return true;
}
function editPersonCardName(card, p) {
  const meta = card.querySelector(".pmeta"); if (!meta) return;
  card.onclick = null;
  card.draggable = false;   // don't let an in-progress rename start a merge-drag
  meta.className = "pmeta pmeta-editing";
  meta.innerHTML = `<input value="${esc(p.name || "")}" placeholder="Person’s name" aria-label="Person’s name">
    <div class="pcount">${p.photos.toLocaleString()} photo${p.photos === 1 ? "" : "s"} · Enter or click away to save</div>`;
  const input = meta.querySelector("input");
  input.onclick = e => e.stopPropagation();
  let finished = false;
  input.addEventListener("blur", () => { if (!finished) { finished = true; savePersonCardName(card, p, input); } });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { finished = true; card.replaceWith(personCard(p)); }
  });
  input.focus(); input.select();
}
async function savePersonCardName(card, p, input) {
  const name = input.value.trim();
  if (name === (p.name || "")) { card.replaceWith(personCard(p)); return; }
  input.disabled = true;
  let result;
  try { result = await jpost("/api/faces/person/rename", { person_id: p.id, name }); }
  catch (e) { result = { error: String(e) }; }
  if (!result || result.error) {
    toast("Couldn’t save the person’s name.", true); card.replaceWith(personCard(p)); return;
  }
  await renderPeopleGrid();
}
const PERSON_PAGE_SIZE = 120;
async function showPerson(id) {
  stopPoll();
  S.section = "people"; renderNav(); S.facePerson = id;
  if (S.arch) location.hash = `/archive/${S.arch.id}/people`;
  const m = document.getElementById("main");
  m.innerHTML = '<div class="muted" style="padding:30px">Loading…</div>';
  const r = await jget(`/api/faces/person/${id}?root=${S.arch.id}&limit=${PERSON_PAGE_SIZE}`);
  if (!r || r.error) { m.innerHTML = '<div class="soonbox">Person not found.</div>'; return; }
  const nm = r.name ? esc(r.name) : "Name this person";
  const nmCls = r.name ? "nm" : "nm un";
  const safe = (r.name || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const avatarFace = (r.items.find(it => it.face_id) || {}).face_id;
  const avatar = avatarFace
    ? `<img class="person-header-avatar" src="/faceThumb/${avatarFace}" alt="" onerror="this.style.visibility='hidden'">`
    : `<div class="person-header-avatar" aria-hidden="true"></div>`;
  m.innerHTML = `<div class="facetopbar">
      <button class="back back-control" type="button" onclick="backToPeople()" aria-label="Back to People">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
        <span>People</span>
      </button>
      ${avatar}
      <div class="ftb-identity">
        <div class="ftb-name" id="personname">
          <button class="person-name-button ${nmCls}" onclick="editPersonName(${id},'${safe}')" title="Rename this person">
            <span>${nm}</span>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.2-1 10.7-10.7a2.1 2.1 0 0 0-3-3L5.2 16 4 20Z"/><path d="m14.5 6.5 3 3"/></svg>
          </button>
        </div>
        <span class="muted ftb-count">${r.photos.toLocaleString()} photo${r.photos === 1 ? "" : "s"}</span>
      </div>
      <button class="not-person-button" type="button" onclick="hidePerson(${id})" title="Mark as a doll, animal, or cartoon and remove from People">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="8" r="3"/><path d="M4 19c.5-3.3 2.5-5 6-5 1.2 0 2.2.2 3 .6M16 15l5 5m0-5-5 5"/></svg>
        <span>Not a person</span>
      </button>
    </div>
    ${mergesPanel(r.merges, "person")}
    <div class="grid" id="grid"></div>
    <div class="infinite-status" id="person-grid-sentinel" aria-live="polite"></div>`;
  let firstPage = r.items;
  startInfiniteList("personDetailList", {
    sentinelId: "person-grid-sentinel", pageSize: PERSON_PAGE_SIZE,
    fetchPage: async offset => {
      if (firstPage) { const page = firstPage; firstPage = null; return page; }
      const res = await jget(`/api/faces/person/${id}?root=${S.arch.id}&offset=${offset}&limit=${PERSON_PAGE_SIZE}`);
      return res.items;
    },
    onPage: (items, { first }) => {
      const grid = document.getElementById("grid");
      if (first) grid.replaceChildren();
      items.forEach(it => grid.appendChild(personTile(it, id)));
    },
  });
}
function backToPeople() { renderFaces(document.getElementById("main")); }
function editPersonName(id, current) {
  const box = document.getElementById("personname"); if (!box) return;
  box.innerHTML = `<input class="detail-name-input" id="personnameinput" value="${esc(current)}" placeholder="Person’s name" aria-label="Person’s name">`;
  const inp = document.getElementById("personnameinput"); inp.focus(); inp.select();
  let finished = false;
  inp.addEventListener("blur", () => { if (!finished) { finished = true; savePersonName(id, inp); } });
  inp.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
    if (e.key === "Escape") { finished = true; showPerson(id); }
  });
}
async function savePersonName(id, inp) {
  const name = inp.value.trim();
  inp.disabled = true;
  let r;
  try { r = await jpost("/api/faces/person/rename", { person_id: id, name }); }
  catch (e) { r = { error: String(e) }; }
  if (!r || r.error) {
    toast((r && r.error) ? ("Couldn’t save: " + r.error) : "Couldn’t save the person’s name.", true);
  }
  showPerson(id);
}
function openPersonFromModal(id) {
  closeModal(); NAV++; S.section = "people"; renderNav(); showPerson(id);
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
const PET_LIST_PAGE_SIZE = 120, LOOSE_PET_PAGE_SIZE = 120, NONHUMAN_PAGE_SIZE = 60;
// Shared by the first render and by syncPetGrids, so an emptied grid says
// the same thing however it got there.
const PET_EMPTY = '<div class="muted">No repeated pets grouped yet.</div>',
      LOOSE_PET_EMPTY = '<div class="muted">No unassigned sightings.</div>',
      NONHUMAN_EMPTY = '<div class="muted">No pending non-human decisions.</div>';
const petStamp = sum => [sum.pets, sum.detections, sum.nonhuman_faces].join("/");
async function renderPets(m) {
  const gen = NAV, root = S.arch.id;
  const sum = await jget("/api/pets/summary?root=" + root);
  if (gen !== NAV) return;
  S.petJobRunning = false; S.petStamp = petStamp(sum);
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Pets</h2>
      <p>Locally detected animals, likely identities, and non-human face review.</p></div></div>
    <div class="statrow">
      <div class="stat"><div class="k">Likely pets</div><div class="v" id="ps-pets">${sum.pets.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Animals</div><div class="v" id="ps-detections">${sum.detections.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Non-human faces</div><div class="v" id="ps-nonhuman">${sum.nonhuman_faces.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Scanned</div><div class="v" id="ps-scanned">${sum.scanned.toLocaleString()} <small>/ ${sum.total_images.toLocaleString()}</small></div></div>
    </div>
    <div class="panel" id="petjob">${detectStatusRow(sum, null)}</div>
    <div class="place-gallery-head"><h3>Likely pet identities</h3><span class="muted">Conservative visual grouping</span></div>
    <div class="people" id="petgrid"></div>
    <div class="infinite-status" id="pet-list-sentinel" aria-live="polite"></div>
    <div class="place-gallery-head"><h3>Unassigned animals</h3><span class="muted">Single or uncertain sightings</span></div>
    <div class="people" id="loosepetgrid"></div>
    <div class="infinite-status" id="loose-pet-sentinel" aria-live="polite"></div>
    <div class="place-gallery-head"><h3>Non-human face review</h3><span class="muted">Animal/toy overlaps filtered out of People</span></div>
    <div class="nonhuman-grid" id="nonhumangrid"></div>
    <div class="infinite-status" id="nonhuman-sentinel" aria-live="polite"></div>`;

  startInfiniteList("petListState", {
    sentinelId: "pet-list-sentinel", pageSize: PET_LIST_PAGE_SIZE,
    fetchPage: async offset => {
      const res = await jget(`/api/pets?root=${root}&offset=${offset}&limit=${PET_LIST_PAGE_SIZE}`);
      return res.pets;
    },
    onPage: (pets, { first, done }) => {
      const petgrid = document.getElementById("petgrid");
      if (first) petgrid.innerHTML = done && !pets.length ? PET_EMPTY : "";
      pets.forEach(p => petgrid.appendChild(petCard(p)));
    },
  });

  startInfiniteList("loosePetState", {
    sentinelId: "loose-pet-sentinel", pageSize: LOOSE_PET_PAGE_SIZE,
    fetchPage: async offset => {
      const res = await jget(`/api/pet/detections?root=${root}&unassigned=1&offset=${offset}&limit=${LOOSE_PET_PAGE_SIZE}`);
      return res.items;
    },
    onPage: (items, { first, done }) => {
      const loosegrid = document.getElementById("loosepetgrid");
      if (first) loosegrid.innerHTML = done && !items.length ? LOOSE_PET_EMPTY : "";
      items.forEach(a => loosegrid.appendChild(looseAnimalCard(a)));
    },
  });

  startInfiniteList("nonhumanState", {
    sentinelId: "nonhuman-sentinel", pageSize: NONHUMAN_PAGE_SIZE,
    fetchPage: async offset => {
      const res = await jget(`/api/nonhuman?root=${root}&offset=${offset}&limit=${NONHUMAN_PAGE_SIZE}`);
      return res.items;
    },
    // Pending is a client-side filter over a confidence-ordered page, so a
    // page can add zero visible cards without the list being done -- the
    // "nothing to review" message only holds once every page is in and
    // still nothing pending turned up. Cards are counted off the grid
    // rather than a running tally, since syncPetGrids also adds and removes
    // them between pages; that also lets a page that overlaps the synced
    // head skip the cards already standing there.
    onPage: (items, { first, done }) => {
      const reviewgrid = document.getElementById("nonhumangrid");
      if (first) reviewgrid.innerHTML = "";
      for (const item of items) {
        if (item.review_status !== "pending") continue;
        if (!reviewgrid.querySelector(`[data-sync-key="${item.id}"]`))
          reviewgrid.appendChild(nonhumanCard(item));
      }
      if (done && !reviewgrid.querySelector(".nonhuman-card"))
        reviewgrid.innerHTML = NONHUMAN_EMPTY;
    },
  });

  startPetPoll();
}
function petMetaInner(p) {
  return `<div class="pname ${p.name ? "" : "un"}">${esc(p.name || "Name this pet")}</div>
      <div class="pcount">${p.photos.toLocaleString()} photo${p.photos === 1 ? "" : "s"}</div>
      <span class="pet-species">${esc(p.species)}</span>`;
}
function petCard(p) {
  const card = document.createElement("div"); card.className = "pcard"; card.onclick = guardCardClick(() => showPet(p.id));
  card.dataset.syncKey = String(p.id);
  card.innerHTML = `<img class="face" src="/animalThumb/${p.cover_detection_id}" data-det="${p.cover_detection_id}" loading="lazy" draggable="false">
      <div class="pmeta">${petMetaInner(p)}</div>`;
  // Pet cards have no inline rename in this grid (rename is a prompt() on
  // the pet detail page), so there's no editing state to guard here.
  // Mutable so syncPetGrids can refresh a renamed/re-counted pet without
  // re-running attachMergeDrag (which would stack a second set of listeners).
  card._merge = { kind: "pet", id: p.id, name: p.name, photos: p.photos };
  attachMergeDrag(card, card._merge, () => showSection("pets", true));
  return card;
}
// In-place refresh of one already-rendered pet card. The cover <img> is
// only reswapped when the cover detection actually changes, so a pet whose
// photo count ticked up mid-run doesn't visibly blink its thumbnail.
function updatePetCard(card, p) {
  const img = card.querySelector("img.face");
  if (img && img.dataset.det !== String(p.cover_detection_id)) {
    img.dataset.det = String(p.cover_detection_id);
    img.src = `/animalThumb/${p.cover_detection_id}`;
  }
  const meta = card.querySelector(".pmeta");
  if (meta) meta.innerHTML = petMetaInner(p);
  Object.assign(card._merge, { name: p.name, photos: p.photos });
  return true;
}
function looseAnimalCard(a) {
  const card = document.createElement("div"); card.className = "pcard"; card.onclick = () => openItem(a.id);
  card.dataset.syncKey = String(a.detection_id);
  card.innerHTML = `<img class="face" src="/animalThumb/${a.detection_id}" loading="lazy">
      <div class="pmeta"><div class="pname un">Unnamed animal</div>
      <div class="pcount">${Math.round(a.score * 100)}% detector confidence</div>
      <span class="pet-species">${esc(a.species)}</span></div>`;
  return card;
}
function nonhumanCard(item) {
  const card = document.createElement("div"); card.className = "nonhuman-card";
  card.dataset.syncKey = String(item.id);
  card.innerHTML = `<img src="/thumb/${item.file_id}" loading="lazy">
      <div class="pcount">${esc(item.kind)} · ${Math.round(item.confidence * 100)}% confidence</div>
      <div class="nonhuman-actions"><button class="btn sec">Confirm non-human</button><button class="btn sec">Actually human</button></div>`;
  const buttons = card.querySelectorAll("button");
  buttons[0].onclick = () => reviewNonhuman(item.id, "confirmed", card);
  buttons[1].onclick = () => reviewNonhuman(item.id, "human", card);
  return card;
}
/* Patch the three pet grids to match the server, without tearing the
   section down. Same reconcile as the people grid (syncCardGrid), keyed by
   pet id / detection id, and with the same 500-row clamp on each endpoint:
   a grid scrolled past that syncs only its first 500 cards, the rest stay
   as they were until the next full render. */
const PET_SYNC_LIMIT = 500;
async function syncPetGrids() {
  if (ACTIVE_SECTION !== "pets" || S.petSyncing) return;   // one in flight at a time
  const st = { pets: S.petListState, loose: S.loosePetState, nonhuman: S.nonhumanState };
  if (!st.pets || !st.loose || !st.nonhuman) return;       // first render still in flight
  const root = S.arch.id;
  const cap = (state, page) => Math.min(PET_SYNC_LIMIT, Math.max(page, state.offset));
  const lim = { pets: cap(st.pets, PET_LIST_PAGE_SIZE),
                loose: cap(st.loose, LOOSE_PET_PAGE_SIZE),
                nonhuman: cap(st.nonhuman, NONHUMAN_PAGE_SIZE) };
  S.petSyncing = true;
  let pets, loose, nonhuman;
  try {
    [pets, loose, nonhuman] = await Promise.all([
      jget(`/api/pets?root=${root}&offset=0&limit=${lim.pets}`).then(r => r.pets),
      jget(`/api/pet/detections?root=${root}&unassigned=1&offset=0&limit=${lim.loose}`).then(r => r.items),
      jget(`/api/nonhuman?root=${root}&offset=0&limit=${lim.nonhuman}`).then(r => r.items),
    ]);
  } catch (e) { return; }
  finally { S.petSyncing = false; }
  const petgrid = document.getElementById("petgrid"),
        loosegrid = document.getElementById("loosepetgrid"),
        reviewgrid = document.getElementById("nonhumangrid");
  // Navigated away, or the section re-rendered, while the fetches were out.
  if (!petgrid || !loosegrid || !reviewgrid) return;
  if (S.petListState !== st.pets || S.loosePetState !== st.loose
      || S.nonhumanState !== st.nonhuman) return;

  // Keep each infinite list's cursor equal to what's on screen, so its next
  // page picks up right after the last card however many were added/pruned.
  let complete = pets.length < lim.pets;
  st.pets.offset = syncCardGrid(petgrid, pets, {
    keyOf: p => p.id, make: petCard, update: updatePetCard, complete, empty: PET_EMPTY });
  if (complete) st.pets.done = (st.pets.offset === pets.length);

  // A detection row never changes once written, so a surviving loose card
  // only ever needs moving -- no updater.
  complete = loose.length < lim.loose;
  st.loose.offset = syncCardGrid(loosegrid, loose, {
    keyOf: a => a.detection_id, make: looseAnimalCard, complete, empty: LOOSE_PET_EMPTY });
  if (complete) st.loose.done = (st.loose.offset === loose.length);

  // The review grid shows only the pending subset of what was fetched, so
  // it reconciles against the filtered list but takes its cursor from the
  // raw page, which is what the infinite list counts. A truncated page
  // leaves that cursor alone: it still points past a tail this sync didn't
  // look at, and onPage skips any card already standing.
  complete = nonhuman.length < lim.nonhuman;
  syncCardGrid(reviewgrid, nonhuman.filter(item => item.review_status === "pending"), {
    keyOf: item => item.id, make: nonhumanCard, complete, empty: NONHUMAN_EMPTY });
  if (complete) { st.nonhuman.offset = nonhuman.length; st.nonhuman.done = true; }
}
function startPetPoll() { stopPoll(); S.poll = setInterval(petTick, 1800); petTick(); }
// Mirrors faceTick: the stat tiles and status row tick every poll, and the
// grids are *patched* (syncPetGrids) rather than rebuilt, so the page never
// resets under the user -- scroll position, the pages the infinite lists
// have already loaded and any half-finished non-human review all survive,
// and only cards whose data actually changed are touched.
async function petTick() {
  if (ACTIVE_SECTION !== "pets") { stopPoll(); return; }
  const area = document.getElementById("petjob"); if (!area) { stopPoll(); return; }
  const [snap, sum] = await Promise.all([
    jget("/api/pipeline?root=" + S.arch.id),
    jget("/api/pets/summary?root=" + S.arch.id)]);
  const running = (snap.stages || []).some(s => s.id === "detect" && s.state === "running");
  const was = S.petJobRunning; S.petJobRunning = running;
  setText("ps-pets", sum.pets.toLocaleString());
  setText("ps-detections", sum.detections.toLocaleString());
  setText("ps-nonhuman", sum.nonhuman_faces.toLocaleString());
  const sc = document.getElementById("ps-scanned");
  if (sc) sc.innerHTML = `${sum.scanned.toLocaleString()} <small>/ ${sum.total_images.toLocaleString()}</small>`;
  area.innerHTML = detectStatusRow(sum, null);
  // Three list endpoints are worth refetching only when something actually
  // moved; the run's finishing edge always gets one last pass.
  const stamp = petStamp(sum);
  if (running ? stamp !== S.petStamp : was) syncPetGrids();
  S.petStamp = stamp;
}
async function reviewNonhuman(id, verdict, card) {
  const result = await jpost("/api/nonhuman/review", { detection_id: id, verdict });
  if (result.error) { toast(result.error, true); return; }
  card.remove();
  toast(verdict === "human" ? "Restored to People for the next clustering pass." : "Confirmed as non-human.");
}
const PET_DETAIL_PAGE_SIZE = 120;
async function showPet(id) {
  stopPoll(); const m = document.getElementById("main");
  m.innerHTML = '<div class="muted" style="padding:30px">Loading…</div>';
  const pet = await jget(`/api/pet/${id}?root=${S.arch.id}&limit=${PET_DETAIL_PAGE_SIZE}`);
  if (!pet || pet.error) { m.innerHTML = '<div class="soonbox">Pet not found.</div>'; return; }
  S.currentPet = pet;
  const name = pet.name || "Name this pet";
  m.innerHTML = `<div class="facetopbar"><button class="back back-control" onclick="showSection('pets',true)">← <span>Pets</span></button>
    <img class="person-header-avatar" src="/animalThumb/${pet.items[0] && pet.items[0].detection_id || 0}" alt="">
    <div class="ftb-identity"><div class="ftb-name"><button class="person-name-button" onclick="renamePet(${pet.id})"><span>${esc(name)}</span></button></div>
    <span class="muted ftb-count">${esc(pet.species)} · ${pet.photos.toLocaleString()} photos</span></div></div>
    ${mergesPanel(pet.merges, "pet")}
    <div class="grid" id="grid"></div>
    <div class="infinite-status" id="pet-grid-sentinel" aria-live="polite"></div>`;
  let firstPage = pet.items;
  startInfiniteList("petDetailList", {
    sentinelId: "pet-grid-sentinel", pageSize: PET_DETAIL_PAGE_SIZE,
    fetchPage: async offset => {
      if (firstPage) { const page = firstPage; firstPage = null; return page; }
      const res = await jget(`/api/pet/${id}?root=${S.arch.id}&offset=${offset}&limit=${PET_DETAIL_PAGE_SIZE}`);
      return res.items;
    },
    onPage: (items, { first }) => {
      const grid = document.getElementById("grid");
      if (first) grid.replaceChildren();
      items.forEach(item => grid.appendChild(tile(item)));
    },
  });
}
async function renamePet(id) {
  const name = prompt("Pet name", (S.currentPet && S.currentPet.name) || ""); if (name === null) return;
  const result = await jpost("/api/pet/rename", { pet_id: id, name: name.trim() });
  if (result.error) { toast(result.error, true); return; } showPet(id);
}

/* ---------- detail modal (editable: faces / place / date) ---------- */
let MITEM = null;                 // the currently-open item, mutated in place on edit

async function openItem(id) {
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

function closeModal() {
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
