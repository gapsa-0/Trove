// Archive setup: what work a new folder gets, and what it is called.
//
// Creation only. This screen used to be reopened months later to change the
// answer, on the theory that adding a feature and choosing one are the same
// decision. They are not: by then the archive has results, and what the
// decision turns on is what each feature actually found in *these* files --
// which is a column this screen cannot fill, because at create time it would be
// prices from top to bottom. That job moved to the Features sheet inside the
// archive (static/js/features.js), and the rules both screens obey moved with
// it (static/js/feature-rules.js), so the two cannot come to disagree.
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
// The shelf holds the whole catalogue, the two undeclinable stages included --
// they carry "Always runs" where the others carry Add. A shelf of only the
// choices described six of the eight things the archive was about to do, and
// left out the two the other six are built on.
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
  cost, costClass, lonelyPair, pendingDownloadMb as downloadMb,
} from "./feature-rules.js";
import {
  jget, jpost,
} from "./api.js";
import {
  featureDocsLink,
} from "./docs.js";
import {
  esc, toast,
} from "./dom.js";
import {
  ICONS,
} from "./state.js";

// The catalogue is app-wide and immutable within a session; the rest is this
// visit's work in progress. `flipped` and `name` live here rather than in the
// DOM so that adding a feature, which rebuilds the whole shelf, does not turn
// back a card that was turned over or discard a name that was half typed.
// Reading them back off the DOM instead is what used to carry the last
// archive's name into the next one: the panel's markup outlives the visit that
// built it.
const SETUP = {
  catalogue: [], chosen: new Set(), flipped: new Set(),
  path: "", name: "", busy: false, done: null,
};

function feature(id) { return SETUP.catalogue.find(f => f.id === id); }

// The feature's mark. The catalogue sends a key rather than a drawing
// (trove/features.py), and it resolves against the same ICONS the
// nav and the Overview health cards draw from — which is the whole point of
// it: the mark on the card you press here is the mark on the card reporting
// that work later, and on the section it unlocks.
function mark(f) { return `<i class="feat-mark" aria-hidden="true">${ICONS[f.icon] || ""}</i>`; }
function folderName(path) { return (path || "").replace(/[/\\]+$/, "").split(/[/\\]/).pop() || path; }

function pendingDownloadMb() { return downloadMb(SETUP.catalogue, SETUP.chosen); }

// `done` is called with the created archive once the save lands. A callback
// rather than an import back into the picker: this module is imported *by* the
// picker, and the cycle would be a real one.
export async function openArchiveSetup(path, done) {
  if (!SETUP.catalogue.length) {
    const { features } = await jget("/api/features");
    SETUP.catalogue = features;
  }
  SETUP.done = done || null;
  SETUP.path = path;
  SETUP.flipped = new Set();
  // Empty, with the folder's own name as the placeholder it falls back to.
  SETUP.name = "";
  // A new archive starts with only the two features it cannot do without, and
  // everything else waiting on the shelf. Starting with all of them ticked
  // would pre-select ~1 GB of model downloads on a screen whose entire purpose
  // is to let someone not pay that.
  SETUP.chosen = new Set(SETUP.catalogue.filter(f => f.required).map(f => f.id));
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
  SETUP.path = ""; SETUP.name = ""; SETUP.busy = false; SETUP.done = null;
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

// Every keystroke in the name field, so that what has been typed survives the
// re-render adding a feature performs. Nothing else is re-rendered for it.
export function setArchiveName(value) { SETUP.name = value; }

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

// Keyed by feature id. A feature with no drawing gets no cover rather than a
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

  // A row per file, each carrying two facts about itself. Not a folder tree:
  // what indexing produces is the catalogue, and the depth it walked is the
  // part nobody has to think about again.
  index: () => ["p1", "p6", "p4"].map(c => `<span class="row">
      <i class="set-ph ${c}"></i><b></b><em></em></span>`).join(""),

  // The same shot three times over, one on top. What the stage does is decide
  // which copy you see; the two behind it are the ones it stops showing you,
  // still there and still whole.
  duplicates: () => `<span class="stack">
      <i class="set-ph p3 c3"></i><i class="set-ph p3 c2"></i>
      <i class="set-ph p3 c1"></i>
      <span class="kept"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 13 5 5L19 7"/></svg></span>
    </span>`,

  // Paper: the characters a file already stores, with the phrase you typed
  // sitting in the middle of them.
  documents: () => `<span class="page">
      <em></em><em></em><em class="hit"></em><em></em><em class="short"></em>
    </span>`,

  // Pixels: the same words, on a picture that stores none of them, with the
  // reader's boxes drawn round what it found. Sibling to the page above, and
  // the difference between them is the whole difference between the two
  // features.
  ocr: () => `<span class="shot">
      <i class="word w1"></i><i class="word w2"></i><i class="word w3"></i>
    </span>`,
};

function preview(f) {
  const draw = PREVIEWS[f.id];
  if (!draw) return "";
  return `<span class="set-pv set-pv-${f.id}">${draw()}</span>`;
}

// ---- rendering -------------------------------------------------------------

function chipItem(f) {
  const out = f.required ? "" : `<button class="set-chip-out" type="button"
        onclick="removeFeature('${f.id}')"
        aria-label="Remove ${esc(f.label)} from this archive">
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3.4 3.4 8.6 8.6M8.6 3.4 3.4 8.6"/></svg>
      </button>`;
  return `<span class="set-chip${f.required ? " fixed" : ""}" data-feature="${f.id}"
      ${f.required ? "" : `draggable="true"`}>
      ${mark(f)}${esc(f.label)}${out}</span>`;
}

// The line that says which of the three kinds of card this is: one you can
// press, one you cannot decline, one this build cannot run.
//
// The required features are on the shelf like everything else. They used to be
// links in the chain and a small note under it, which left the one screen whose
// job is deciding what runs describing six of the eight things it was about to
// do -- and the two it skipped are the two the rest is built on. A card each
// says what they are for in the same place, and in the same words, as the six.
function pill(f) {
  if (f.required) return `<span class="set-always">Always runs</span>`;
  if (!f.available) return `<span class="set-unavailable">Not in this build</span>`;
  const chosen = SETUP.chosen.has(f.id);
  return `<button class="set-add" type="button" aria-pressed="${chosen}">${chosen
    ? `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 13 5 5L19 7"/></svg>On`
    : "Add"}</button>`;
}

// Whether pressing this card would do anything. False for the two nobody may
// switch off and for anything this build cannot run, and it decides all three
// of the ways a card offers itself: the click, the drag, and the lift under the
// pointer that promises both.
function pressable(f) { return !f.required && f.available; }

function cardItem(f) {
  const chosen = SETUP.chosen.has(f.id);
  const back = SETUP.flipped.has(f.id);
  const { text } = cost(f);
  return `<li class="set-card${chosen ? " on" : ""}${f.available ? "" : " off"}${f.required
    ? " fixed" : ""}" data-feature="${f.id}" draggable="${pressable(f)}">
      <div class="set-face"${back ? " hidden" : ""}${pressable(f)
    ? ` onclick="toggleFeature('${f.id}')"` : ""}>
        <span class="set-cover">${preview(f)}</span>
        <div class="set-meta">
          <span class="set-card-name">${mark(f)}${esc(f.label)}</span>
          <p class="set-card-line">${esc(f.tagline)}</p>
          <button class="set-flip" type="button"
            onclick="event.stopPropagation();flipFeature('${f.id}')">More info</button>
          <div class="set-card-foot">
            <span class="${costClass(f)}">${text}</span>
            ${pill(f)}
          </div>
        </div>
      </div>
      <div class="set-face set-back"${back ? "" : " hidden"}>
        <span class="set-card-name">${mark(f)}${esc(f.label)}</span>
        <p class="set-card-detail">${esc(f.detail)}</p>
        <div class="set-back-foot">
          <button class="set-flip" type="button" onclick="flipFeature('${f.id}')">Back</button>
          ${featureDocsLink(f.id)}
        </div>
      </div>
    </li>`;
}

// The chain, plus the open end that invites the first drop. The figure is the
// only part set as data; the prose around it is a sentence, not a measurement.
//
// Each link carries the connector that *follows* it rather than the two being
// laid out as siblings, because the chain wraps (see .set-flow) and the pair
// has to wrap together: a connector that breaks away from its own chip opens
// the next row with a line pointing at nothing.
//
// The empty <svg> is where the chain turns the corner -- drawn rather than laid
// out, because only the browser knows where a flexbox chose to wrap. See
// drawChainTurns.
function pipeline(live, waiting) {
  const links = live.map(chipItem);
  if (waiting.length) links.push(`<span class="set-slot">Drop a feature here</span>`);
  return links.map((link, at) => `<span class="set-step">${link}${at < links.length - 1
    ? `<span class="set-link" aria-hidden="true"></span>` : ""}</span>`).join("")
    + `<svg class="set-turns" aria-hidden="true"></svg>`;
}

// Where the chain changes rows, drawn as one stroke: out of the last chip on
// the row, along to the panel's edge, down into the gap, back across to the
// left, and down into the top of the chip the chain carries on from.
//
// The whole screen rests on the pipeline being a chain rather than a menu, and
// a chain that stops at one margin and starts again at the other is two chains.
// The row used to end in a dangling connector, which is a hyphen doing a link's
// job.
//
// It has to be measured. A flexbox decides where to wrap from the widths of
// eight labels against whatever the window is, and no CSS selector can name the
// chip that ended up last on a row -- so this runs after each render and on
// every resize, and draws nothing at all while the chain fits on one line.
//
// The return travels through the *gap* between the rows and enters the next
// chip from above. At either row's own height it would cross the chips it is
// there to join, and it cannot come in from the left because a row starts hard
// against the panel's edge, with no room to turn in.
function drawChainTurns(flow) {
  const svg = flow.querySelector(".set-turns");
  if (!svg) return;
  const steps = [...flow.querySelectorAll(".set-step")];
  const box = flow.getBoundingClientRect();
  const paths = [];
  for (let at = 0; at < steps.length - 1; at++) {
    const from = steps[at].getBoundingClientRect();
    const to = steps[at + 1].getBoundingClientRect();
    if (to.top < from.bottom - 1) continue;          // same row: its own link joins them
    // Out of the connector this step already carries, rather than out of the
    // chip: the two are one line, and the corner is where it goes next.
    const x0 = from.right - box.left, y0 = from.top - box.top + from.height / 2;
    const edge = box.width - 1, top = to.top - box.top;
    const turn = (from.bottom - box.top + top) / 2;  // mid-gap, clear of both rows
    const into = to.left - box.left + 17;            // under the next chip's mark
    const r = Math.max(1, Math.min(7, edge - x0, turn - y0, edge - into, top - turn));
    paths.push(`<path d="M${x0} ${y0}H${edge - r}Q${edge} ${y0} ${edge} ${y0 + r}`
      + `V${turn - r}Q${edge} ${turn} ${edge - r} ${turn}`
      + `H${into + r}Q${into} ${turn} ${into} ${turn + r}V${top}"/>`);
  }
  svg.innerHTML = paths.join("");
}

// One observer for the life of the module, re-pointed at each render's chain --
// which is a new element every time, so what it draws into is held here rather
// than captured: a callback closed over the first render's chain would go on
// drawing into a node that had left the document.
//
// Watching the chain rather than the window catches every way the wrap can move
// -- the panel resized, the app's own sidebar collapsing -- and the turns are
// absolutely positioned, so drawing them cannot change what is being measured.
let CHAIN_WATCH = null, CHAIN_FLOW = null;
function watchChain(flow) {
  CHAIN_FLOW = flow;
  drawChainTurns(flow);
  if (!CHAIN_WATCH) {
    CHAIN_WATCH = new ResizeObserver(() => CHAIN_FLOW && drawChainTurns(CHAIN_FLOW));
  }
  CHAIN_WATCH.disconnect();
  CHAIN_WATCH.observe(flow);
}

// Half of a pair running without the other half. Both halves name each other,
// so this is said once, about the chain, rather than on both cards.
function pairNote() {
  const pair = lonelyPair(SETUP.catalogue, SETUP.chosen);
  if (!pair) return "";
  const [lonely, partner] = pair;
  return `<p class="set-pair">${esc(lonely.label)} is running without ${esc(partner.label)}.
    The two check each other's work, so having both makes each of them more accurate.</p>`;
}

// The chain's one line of explanation: which links cannot be taken out, and
// what that buys the rest. Composed from the catalogue rather than typed, so a
// third undeclinable stage would be named here instead of being quietly left
// out of a sentence about two. What each of them actually does is on its own
// card, in the same words as the six beside it.
function trunkNote() {
  const fixed = SETUP.catalogue.filter(f => f.required).map(f => esc(f.label));
  if (!fixed.length) return "";
  const named = fixed.length > 1
    ? `${fixed.slice(0, -1).join(", ")} and ${fixed[fixed.length - 1]}`
    : fixed[0];
  const many = fixed.length > 1;
  return `<p class="set-pipe-note">${named} always ${many ? "run" : "runs"}.
    Every other stage reads what ${many ? "they produce" : "it produces"}.</p>`;
}

function totalLine() {
  const mb = pendingDownloadMb();
  return mb
    ? `<b>${mb} MB</b>, downloaded once on the first run. After that Trove works offline.`
    : `<b>0 MB</b> to download. This archive starts work as soon as you create it.`;
}

function renderSetup(landed) {
  const live = SETUP.catalogue.filter(f => SETUP.chosen.has(f.id));
  const waiting = SETUP.catalogue.filter(f => pressable(f) && !SETUP.chosen.has(f.id));
  document.getElementById("setup-body").innerHTML = `
    <div class="set-head">
      <div>
        <div class="set-eyebrow">New archive</div>
        <h1>Choose what Trove does here</h1>
        <p class="set-sub">Pick what you want now. Once the archive is open you can change
          any of this from its Library health panel, and nothing in the folder is ever moved,
          renamed or deleted.</p>
        <p class="set-path" title="${esc(SETUP.path)}">${esc(SETUP.path)}</p>
      </div>
      <label class="set-name">
        <span>Name this archive</span>
        <input id="setup-name" value="${esc(SETUP.name)}" oninput="setArchiveName(this.value)"
               placeholder="${esc(folderName(SETUP.path))}" maxlength="80">
      </label>
    </div>

    <section class="set-pipe" id="set-pipe" aria-labelledby="set-pipe-h">
      <header class="set-pipe-head">
        <span class="set-label" id="set-pipe-h">This archive runs</span>
        <span class="set-pipe-mb">${pendingDownloadMb()} MB</span>
      </header>
      <div class="set-flow" id="set-flow">${pipeline(live, waiting)}</div>
      ${trunkNote()}
      ${pairNote()}
    </section>

    <section class="set-shelf" id="set-shelf" aria-labelledby="set-shelf-h">
      <header class="set-shelf-head">
        <span class="set-label" id="set-shelf-h">${waiting.length
    ? "Add to the pipeline" : "Everything is switched on"}</span>
        <em>${waiting.length ? "Drag a card onto the pipeline, or press Add" : ""}</em>
      </header>
      <ul class="set-cards">${SETUP.catalogue.map(cardItem).join("")}</ul>
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
          Create archive</button>
      </div>
    </div>`;
  wireDragAndDrop();
  watchChain(document.getElementById("set-flow"));
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
  const name = SETUP.name.trim();
  const features = SETUP.catalogue.filter(f => SETUP.chosen.has(f.id)).map(f => f.id);
  const result = await jpost("/api/archives", { path: SETUP.path, name, features });
  SETUP.busy = false;
  if (result.error) { toast(result.error, true); return; }
  // The caller decides what happens next: the picker reloads, and the archive
  // just created is opened.
  const done = SETUP.done;
  closeArchiveSetup();
  if (done) done(result);
}
