// Archive setup: what work this folder gets, and what it is called.
//
// The screen is built around one true thing: the pipeline is a chain, not a
// menu. Indexing and Duplicates are the trunk everything else reads from, so
// they sit in the chain and cannot be taken out of it; every optional feature
// clips on after them. The chain is drawn small, at the top, because it is a
// summary of what was chosen and not the thing you operate.
//
// What you operate is the shelf of cards below it. Each cover shows what its
// feature produces rather than describing it, and each card turns over to a
// description of what it does. Cards are a fixed height and turn in place, so
// reading one never moves the others.
//
// Dragging a card onto the chain is the same action as pressing Add, and
// dragging a link back down to the shelf is the same as pressing its remove
// button, because a drag-only interface is unusable with a keyboard and
// awkward on a trackpad.
//
// The running download figure on the chain is the other true thing: weights
// are shared between archives (see paths.py), so the second archive that wants
// People pays nothing, and the panel says so rather than quoting a download
// that will not happen.

import {
  jget, jpost,
} from "./api.js";
import {
  esc, toast,
} from "./dom.js";

// The catalogue is app-wide and immutable within a session; the rest is this
// visit's work in progress. `archive` is null while adding a folder that has
// no id yet, which is also what tells Save whether to create or reconfigure.
// `flipped` lives here rather than in the DOM so that adding a feature, which
// rebuilds the whole shelf, does not turn back a card that was turned over.
const SETUP = {
  catalogue: [], chosen: new Set(), flipped: new Set(),
  archive: null, path: "", busy: false, done: null,
};

export function isSetupOpen() { return SETUP.path !== "" || SETUP.archive !== null; }

function feature(id) { return SETUP.catalogue.find(f => f.id === id); }
function folderName(path) { return (path || "").replace(/[/\\]+$/, "").split(/[/\\]/).pop() || path; }

// What the first run of this archive will actually download. A feature whose
// weights are already on disk contributes nothing, which is the difference
// between an honest figure and a scary one.
function pendingDownloadMb() {
  return SETUP.catalogue
    .filter(f => SETUP.chosen.has(f.id) && !f.ready)
    .reduce((total, f) => total + f.download_mb, 0);
}

// `done` is called with the created archive (or null after a reconfigure) once
// the save lands. A callback rather than an import back into the picker: this
// module is imported *by* the picker, and the cycle would be a real one.
export async function openArchiveSetup(archive, path, done) {
  if (!SETUP.catalogue.length) {
    const { features } = await jget("/api/features");
    SETUP.catalogue = features;
  }
  SETUP.archive = archive || null;
  SETUP.done = done || null;
  SETUP.path = archive ? archive.path : path;
  SETUP.flipped = new Set();
  // A new archive starts with only the two features it cannot do without, and
  // everything else waiting on the shelf. Starting with all of them ticked
  // would pre-select ~1 GB of model downloads on a screen whose entire purpose
  // is to let someone not pay that.
  SETUP.chosen = new Set(
    archive ? archive.features : SETUP.catalogue.filter(f => f.required).map(f => f.id),
  );
  document.getElementById("picker").style.display = "none";
  // Explicit `block`, not "": the stylesheet's own rule for #setup is
  // `display: none`, so clearing the inline value would hand the element back
  // to a rule that hides it.
  const screen = document.getElementById("setup");
  screen.style.display = "block";
  renderSetup();
  screen.querySelector("#setup-name").focus();
}

export function closeArchiveSetup() {
  SETUP.archive = null; SETUP.path = ""; SETUP.busy = false; SETUP.done = null;
  SETUP.flipped = new Set();
  document.getElementById("setup").style.display = "none";
  document.getElementById("picker").style.display = "";
}

export function addFeature(id) {
  const f = feature(id);
  if (!f || !f.available || SETUP.chosen.has(id)) return;
  SETUP.chosen.add(id);
  renderSetup(id);
}

export function removeFeature(id) {
  const f = feature(id);
  if (!f || f.required || !SETUP.chosen.has(id)) return;
  SETUP.chosen.delete(id);
  renderSetup();
}

// Clicking a card's front is the same as pressing its pill.
export function toggleFeature(id) {
  if (SETUP.chosen.has(id)) removeFeature(id); else addFeature(id);
}

// Turned over in place rather than by re-rendering the shelf, so focus can be
// handed straight to the button that replaces the one just pressed.
export function flipFeature(id) {
  const card = document.querySelector(`.set-card[data-feature="${id}"]`);
  if (!card) return;
  card.querySelectorAll(".set-face").forEach(face => { face.hidden = !face.hidden; });
  if (SETUP.flipped.has(id)) SETUP.flipped.delete(id); else SETUP.flipped.add(id);
  const flip = card.querySelector(".set-face:not([hidden]) .set-flip");
  if (flip) flip.focus();
}

// ---- previews --------------------------------------------------------------

// Subjects as they sit in a crop: head and shoulders for a person, pricked ears
// for a cat, drooping ones for a dog. Not a paw print. A paw is a symbol
// meaning "pet", where what the Pets page shows you is the animal's face, and
// the two groups it finds most of are cats and dogs.
const HEAD = `<svg viewBox="0 0 30 22" aria-hidden="true"><circle cx="15" cy="7" r="6"/><path d="M2 22c1.6-6.4 7-9.5 13-9.5S26.4 15.6 28 22z"/></svg>`;
const CAT = `<svg viewBox="0 0 30 22" aria-hidden="true"><path d="M7.6 9.4 6.2 2.4l5.9 3.6a11.6 11.6 0 0 1 5.8 0l5.9-3.6-1.4 7a9.2 9.2 0 0 1 1.7 5.3c0 4.5-4.5 8.1-9.1 8.1s-9.1-3.6-9.1-8.1a9.2 9.2 0 0 1 1.7-5.3Z"/></svg>`;
const DOG = `<svg viewBox="0 0 30 22" aria-hidden="true"><ellipse cx="5.9" cy="14.1" rx="3.6" ry="6.6" transform="rotate(-13 5.9 14.1)"/><ellipse cx="24.1" cy="14.1" rx="3.6" ry="6.6" transform="rotate(13 24.1 14.1)"/><path d="M15 5.6c4.2 0 7.5 3.2 7.5 7.4 0 4.9-3.4 8.8-7.5 8.8s-7.5-3.9-7.5-8.8c0-4.2 3.3-7.4 7.5-7.4Z"/></svg>`;

// Keyed by feature id, and only the optional ones have an entry: the two that
// always run are links in the chain and never get a card, so a drawing for
// them would be dead. A feature with no drawing gets no cover rather than a
// grey box, so adding one to features.py cannot break this screen.
const PREVIEWS = {
  people: () => `<span class="set-pv-stage">
      ${[["p1", "Ana"], ["p5", "Marco"], ["p4", "Lucía"]].map(([c, n]) =>
    `<span class="set-face-chip"><i class="set-ph ${c}">${HEAD}</i><span>${n}</span></span>`).join("")}
      <span class="set-face-chip"><i class="set-more">+34</i><span>more</span></span>
    </span>`,

  pets: () => `<span class="set-pv-stage">
      ${[["p3", "Nube", CAT], ["p2", "Tomás", DOG]].map(([c, n, shape]) =>
    `<span class="set-face-chip"><i class="set-ph ${c}">${shape}</i><span>${n}</span></span>`).join("")}
      <span class="set-face-chip"><i class="set-more">+2</i><span>more</span></span>
    </span>`,

  places: () => `<span class="set-pv-map">
      <span class="road r1"></span><span class="road r3"></span><span class="road r2"></span>
      <span class="pin" style="left:22%;top:42%"></span>
      <span class="pin" style="left:74%;top:33%"></span>
      <span class="pin" style="left:60%;top:76%"></span>
      <span class="cluster" style="left:42%;top:60%">18</span>
    </span>`,

  semantic: () => `<span class="q">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4.5 4.5"/></svg>
      <span class="qt">a dog on the beach</span><span class="caret"></span>
    </span>
    <span class="hits"><i class="set-ph p3"></i><i class="set-ph p1"></i><i class="set-ph p5"></i></span>`,
};

function preview(f) {
  const draw = PREVIEWS[f.id];
  if (!draw) return "";
  return `<span class="set-pv set-pv-${f.id}">${draw()}</span>`;
}

// ---- rendering -------------------------------------------------------------

// What a feature costs to switch on, said in as few words as it deserves. The
// megabyte figure is the only one set as data, because it is the only one that
// is a measurement; the rest are states.
function cost(f) {
  if (!f.download_mb) return { text: "no download", figure: false };
  if (f.ready) return { text: "downloaded", figure: false };
  return { text: `${f.download_mb} MB`, figure: true };
}

function chipItem(f) {
  const out = f.required ? "" : `<button class="set-chip-out" type="button"
        onclick="removeFeature('${f.id}')"
        aria-label="Remove ${esc(f.label)} from this archive">
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3.4 3.4 8.6 8.6M8.6 3.4 3.4 8.6"/></svg>
      </button>`;
  return `<span class="set-chip${f.required ? " fixed" : ""}" data-feature="${f.id}"
      ${f.required ? "" : `draggable="true"`}>
      <span class="set-node" aria-hidden="true"></span>${esc(f.label)}${out}</span>`;
}

function cardItem(f) {
  const chosen = SETUP.chosen.has(f.id);
  const back = SETUP.flipped.has(f.id);
  const { text, figure } = cost(f);
  const pill = f.available
    ? `<button class="set-add" type="button" aria-pressed="${chosen}">${chosen
      ? `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 13 5 5L19 7"/></svg>On`
      : "Add"}</button>`
    : `<span class="set-unavailable">Not in this build</span>`;
  return `<li class="set-card${chosen ? " on" : ""}${f.available ? "" : " off"}"
      data-feature="${f.id}" draggable="${f.available}">
      <div class="set-face"${back ? " hidden" : ""}${f.available
    ? ` onclick="toggleFeature('${f.id}')"` : ""}>
        <span class="set-cover">${preview(f)}</span>
        <div class="set-meta">
          <span class="set-card-name">${esc(f.label)}</span>
          <p class="set-card-line">${esc(f.tagline)}</p>
          <button class="set-flip" type="button"
            onclick="event.stopPropagation();flipFeature('${f.id}')">What this does</button>
          <div class="set-card-foot">
            <span class="set-cost${figure ? " figure" : ""}">${text}</span>
            ${pill}
          </div>
        </div>
      </div>
      <div class="set-face set-back"${back ? "" : " hidden"}>
        <span class="set-card-name">${esc(f.label)}</span>
        <p class="set-card-detail">${esc(f.detail)}</p>
        <button class="set-flip" type="button" onclick="flipFeature('${f.id}')">Back</button>
      </div>
    </li>`;
}

// The chain, plus the open end that invites the first drop. The figure is the
// only part set as data; the prose around it is a sentence, not a measurement.
function pipeline(live, waiting) {
  const links = live.map(chipItem);
  if (waiting.length) links.push(`<span class="set-slot">Drop a feature here</span>`);
  return links.join(`<span class="set-link" aria-hidden="true"></span>`);
}

// Half of a pair running without the other half. Both halves name each other,
// so this is said once, about the chain, rather than on both cards.
function pairNote(live) {
  const lonely = live.find(f => f.pairs_with && !SETUP.chosen.has(f.pairs_with));
  if (!lonely) return "";
  const partner = feature(lonely.pairs_with);
  if (!partner || !partner.available) return "";
  return `<p class="set-pair">${esc(lonely.label)} is running without ${esc(partner.label)}.
    The two check each other's work, so having both makes each of them more accurate.</p>`;
}

function totalLine() {
  const mb = pendingDownloadMb();
  return mb
    ? `<b>${mb} MB</b>, downloaded once on the first run. After that Trove works offline.`
    : `<b>0 MB</b> to download. This archive starts work as soon as you create it.`;
}

function renderSetup(landed) {
  const editing = SETUP.archive !== null;
  const live = SETUP.catalogue.filter(f => SETUP.chosen.has(f.id));
  const optional = SETUP.catalogue.filter(f => !f.required);
  const waiting = optional.filter(f => !SETUP.chosen.has(f.id) && f.available);
  const name = document.getElementById("setup-name");
  const typed = name && editing === false && name.value ? name.value : null;
  document.getElementById("setup-body").innerHTML = `
    <div class="set-head">
      <div>
        <div class="set-eyebrow">${editing ? "Archive setup" : "New archive"}</div>
        <h1>${editing ? "Change what Trove does with this archive"
    : "Choose what Trove does here"}</h1>
        <p class="set-sub">${editing
    ? "Adding a feature picks up from what is already catalogued, and removing one keeps whatever it found."
    : "Pick what you want now. You can turn features off or add more later, and nothing in the folder is ever moved, renamed or deleted."}</p>
        <p class="set-path" title="${esc(SETUP.path)}">${esc(SETUP.path)}</p>
      </div>
      <label class="set-name">
        <span>Name this archive</span>
        <input id="setup-name" value="${esc(typed ?? (editing ? SETUP.archive.name : ""))}"
               placeholder="${esc(folderName(SETUP.path))}" maxlength="80">
      </label>
    </div>

    <section class="set-pipe" id="set-pipe" aria-labelledby="set-pipe-h">
      <header class="set-pipe-head">
        <span class="set-label" id="set-pipe-h">This archive runs</span>
        <span class="set-pipe-mb">${pendingDownloadMb()} MB</span>
      </header>
      <div class="set-flow" id="set-flow">${pipeline(live, waiting)}</div>
      <p class="set-pipe-note">Indexing and Duplicates always run. Every other stage reads
        what they produce.</p>
      ${pairNote(live)}
    </section>

    <section class="set-shelf" id="set-shelf" aria-labelledby="set-shelf-h">
      <header class="set-shelf-head">
        <span class="set-label" id="set-shelf-h">${waiting.length
    ? "Add to the pipeline" : "Everything is switched on"}</span>
        <em>${waiting.length ? "Drag a card onto the pipeline, or press Add" : ""}</em>
      </header>
      <ul class="set-cards">${optional.map(cardItem).join("")}</ul>
      <p class="set-privacy">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.7 4.8 5.8v5.3c0 4.5 3 8.6 7.2 9.9 4.2-1.3 7.2-5.4 7.2-9.9V5.8Z"/></svg>
        <span>Every stage runs on this machine: no photo, no face and nothing you type ever
          leaves it. The only things fetched from the internet are the models themselves, once,
          and the map's street layer if you switch that on.</span>
      </p>
    </section>

    <div class="set-foot">
      <div>
        <p class="set-total">${totalLine()}</p>
        <p class="set-count">${live.length} of ${SETUP.catalogue.length} features on.</p>
      </div>
      <div class="set-actions">
        <button class="btn sec" type="button" onclick="closeArchiveSetup()">Cancel</button>
        <button class="btn" type="button" onclick="submitArchiveSetup()"${SETUP.busy ? " disabled" : ""}>
          ${editing ? "Save changes" : "Create archive"}</button>
      </div>
    </div>`;
  if (typed !== null) document.getElementById("setup-name").value = typed;
  wireDragAndDrop();
  if (landed) flashLanded(landed);
}

// A feature that just joined the chain is briefly outlined where it landed: the
// card it came from stays put on the shelf, so without this nothing marks the
// arrival.
function flashLanded(id) {
  const chip = document.querySelector(`#set-flow .set-chip[data-feature="${id}"]`);
  if (!chip) return;
  chip.classList.add("landed");
  setTimeout(() => chip.classList.remove("landed"), 500);
}

// Cards drag onto the chain to add; links drag back onto the shelf to remove.
// Both elements are rebuilt by every render, so the listeners go on the two
// containers, which are rebuilt with them.
function wireDragAndDrop() {
  const pipe = document.getElementById("set-pipe");
  const shelf = document.getElementById("set-shelf");
  const targets = [[pipe, true], [shelf, false]];

  [[shelf, ".set-card[draggable=true]"], [pipe, ".set-chip[draggable]"]].forEach(([box, sel]) => {
    box.addEventListener("dragstart", event => {
      const held = event.target.closest(sel);
      if (!held) return;
      event.dataTransfer.setData("text/plain", held.dataset.feature);
      event.dataTransfer.effectAllowed = "move";
      held.classList.add("dragging");
      // Only the target that would change something is offered.
      (SETUP.chosen.has(held.dataset.feature) ? shelf : pipe).classList.add("drop-open");
    });
    box.addEventListener("dragend", () => {
      document.querySelectorAll("#setup .dragging").forEach(el => el.classList.remove("dragging"));
      targets.forEach(([el]) => el.classList.remove("drop-open", "drop-over"));
    });
  });

  targets.forEach(([target, adds]) => {
    target.addEventListener("dragover", event => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      target.classList.add("drop-over");
    });
    target.addEventListener("dragleave", event => {
      if (!target.contains(event.relatedTarget)) target.classList.remove("drop-over");
    });
    target.addEventListener("drop", event => {
      event.preventDefault();
      targets.forEach(([el]) => el.classList.remove("drop-open", "drop-over"));
      const id = event.dataTransfer.getData("text/plain");
      // Dropping something back where it already was is not a change.
      if (feature(id)) (adds ? addFeature : removeFeature)(id);
    });
  });
}

// ---- saving ----------------------------------------------------------------

export async function submitArchiveSetup() {
  if (SETUP.busy) return;
  SETUP.busy = true;
  const name = document.getElementById("setup-name").value.trim();
  const features = SETUP.catalogue.filter(f => SETUP.chosen.has(f.id)).map(f => f.id);
  const editing = SETUP.archive !== null;
  const body = editing
    ? { root_id: SETUP.archive.id, name, features }
    : { path: SETUP.path, name, features };
  const result = await jpost(editing ? "/api/archive/configure" : "/api/archives", body);
  SETUP.busy = false;
  if (result.error) { toast(result.error, true); return; }
  // The caller decides what happens next: the picker reloads either way, and a
  // freshly created archive is opened.
  const done = SETUP.done;
  closeArchiveSetup();
  if (done) done(editing ? null : result);
}
