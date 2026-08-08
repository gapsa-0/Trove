// The start page: the gallery of registered archives, adding one by folder, and
// opening it. This is the only screen that exists before an archive is chosen,
// so it is also where the app's very first render happens.

import {
  resetSectionViews, showSection,
} from "./router.js";
import {
  jget, jpost,
} from "./api.js";
import {
  esc, fmtBytes, toast,
} from "./dom.js";
import {
  S,
} from "./state.js";
import {
  startPipelinePoll,
} from "./pipeline.js";
import {
  openArchiveSetup,
} from "./setup.js";

export let ARCHIVES = [];
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
export async function loadPicker() {
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
    // So a folder that is refused for already being an archive can point at
    // the card it is: knowing which of these it already is beats being told
    // that it is one of them.
    c.dataset.archive = a.id;
    c.onclick = () => openArchive(a);
    c.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openArchive(a); } };
    const warn = a.exists ? "" : ` · <span class="warn">not mounted</span>`;
    // What the card counts is what has been catalogued, and a scan that has not
    // finished has more of it to come -- so until one has, the count and the
    // byte badge beside it are a floor rather than the size of the archive.
    // Said once, on the sentence, since it governs both figures; the badge
    // stays a quantity. It covers an interrupted scan as honestly as a running
    // one, which is why it is not "still scanning".
    const sofar = a.partial ? " so far" : "";
    c.innerHTML = pickerCover(a) +
      `<div class="p-meta">
             <button class="p-remove" type="button" aria-label="Remove archive">Remove</button>
             <button class="p-rename" type="button" aria-label="Rename ${esc(a.name)}">Rename</button>
             <div class="nm">${esc(a.name)}</div>
             <div class="st">${a.files.toLocaleString()} files${sofar}${warn}</div>
           </div>`;
    c.querySelector(".p-remove").onclick = (event) => { event.stopPropagation(); removeArchive(a); };
    // What this card used to offer was "Set up", which reopened the whole
    // creation screen to change one of two unrelated things. What an archive
    // *runs* is now changed from inside it, where its results are, and what is
    // left here is the one property that belongs to a folder in a list.
    c.querySelector(".p-rename").onclick = (event) => { event.stopPropagation(); startRename(a, c); };
    el.appendChild(c);
  });
  const add = document.createElement("button");
  add.className = "p-card add"; add.type = "button";
  add.innerHTML = `<span>+</span>${archives.length ? "Add another folder" : "Add your first folder"}`;
  add.onclick = () => startAddArchive();
  el.appendChild(add);
}
// Renamed in place, the way a person and a place are renamed, rather than on a
// screen of its own: the name is one field, and it is already on screen.
function startRename(a, card) {
  const box = card.querySelector(".nm");
  if (!box || box.querySelector("input")) return;
  box.innerHTML = `<input class="p-name-input" value="${esc(a.name)}" maxlength="80"
      aria-label="Archive name">`;
  const input = box.querySelector("input");
  let finished = false;
  // The card behind this field is itself a button that opens the archive, and
  // both clicks and keys bubble to it: without these, selecting a word would
  // open the archive and typing a space would too.
  input.onclick = event => event.stopPropagation();
  input.onkeydown = event => {
    event.stopPropagation();
    if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    if (event.key === "Escape") { finished = true; loadPicker(); }
  };
  input.onblur = () => { if (!finished) { finished = true; saveArchiveName(a, input.value); } };
  input.focus(); input.select();
}
async function saveArchiveName(a, value) {
  const name = value.trim();
  // An empty field is a cancelled rename, not a request for a nameless
  // archive: the server would take it and the card would lose its title.
  if (!name || name === a.name) { loadPicker(); return; }
  const r = await jpost("/api/archive/configure", { root_id: a.id, name })
    .catch(() => ({ error: "Couldn’t rename this archive." }));
  if (!r || r.error) toast((r && r.error) || "Couldn’t rename this archive.", true);
  loadPicker();
}
async function startAddArchive() {
  const field = document.getElementById("archive-path"); let p = field.value.trim();
  if (window.archiveDesktop?.chooseFolder) {
    const picked = await window.archiveDesktop.chooseFolder();
    if (picked.cancelled) return false;
    p = picked.path || ""; field.value = p;
  }
  if (!p) { highlightAddArchiveField(); return false; }
  // Asked here, where the folder is chosen, rather than left to the save at the
  // end of setup. Both refusals -- not a folder, already an archive -- are
  // true the moment it is picked and have nothing to do with what gets
  // configured, so finding out after choosing eight features is being sent
  // back to the start for something that was knowable before it began.
  //
  // The server answers, not this list: which folder a path really is depends on
  // symlinks and on how it resolves, and comparing the strings here would let
  // the same folder in twice by a different name.
  const answer = await jget("/api/archives/check?path=" + encodeURIComponent(p))
    .catch(() => ({ error: "Couldn’t check that folder." }));
  if (!answer || answer.error) {
    toast((answer && answer.error) || "Couldn’t check that folder.", true);
    if (answer && answer.archive_id) flashArchiveCard(answer.archive_id);
    else highlightAddArchiveField();
    return false;
  }
  // The folder is not registered here any more: setup is where the archive is
  // created, because the features chosen there decide what gets downloaded and
  // an archive that existed first would already be scanning under the default.
  field.value = "";
  await openArchiveSetup(p, afterSetup);
  return false;
}
// What happens once setup saves. Passed in rather than imported back, so the
// dependency between these two modules stays one-way.
async function afterSetup(created) {
  await loadPicker();
  const a = ARCHIVES.find(x => x.id === created.id)
    || { ...created, files: 0, size: 0, partial: true, exists: true, covers: [] };
  openArchive(a, "overview");
}
/* The card for a folder that turned out to be one of these already. Scrolled to
   and outlined for a moment, so the answer is the archive itself rather than a
   sentence about it. */
function flashArchiveCard(id) {
  const card = document.querySelector(`.p-card[data-archive="${id}"]`);
  if (!card) return;
  card.scrollIntoView({ block: "center", behavior: "smooth" });
  card.classList.add("found");
  clearTimeout(card._foundTimer);
  card._foundTimer = setTimeout(() => card.classList.remove("found"), 1600);
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
export async function addArchiveFromForm(event) {
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
/* Async because the open has to LAND before the section draws.

   Thumbnails and originals are served by bare id and resolve against whichever
   archive the server has open (routes/media.py), so a grid that renders before
   this POST arrives asks for pictures the server will not admit exist, and gets
   404s it never retries -- a screen of broken tiles, or a viewer whose photo
   never appears. It was fire-and-forget and usually won the race on a local
   socket, which is exactly what made the failure rare and baffling.

   Callers do not await it: what matters is the ordering inside. */
export async function openArchive(a, section) {
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
  await jpost("/api/archive/open", { root_id: a.id });
  showSection(S.section); startPipelinePoll();
}
