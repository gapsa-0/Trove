// The Features sheet: the catalogue of what this archive can do, and what it
// currently does.
//
// A fork of the setup screen, and deliberately one -- but a fork of its
// framing, not of its card. The two screens answer different questions. Setup
// asks "what should this folder become", of someone who has not seen a single
// result yet. This asks "is this still what I want", of someone holding the
// evidence: forty thousand files catalogued, four animals found, no locations
// at all. So every card here carries one fact its twin cannot -- what the
// feature has given *this* archive, or what it would cost to switch on -- in
// the slot where the setup card puts its price.
//
// Everything else about the card is the setup card, down to the class names:
// the same fixed height, the same turn to read what a feature does, the same
// link out to its page on the back. Two catalogues of one set of features that
// looked like two different products would be the real drift, so the shell is
// shared (see setup.css) and only what differs lives here.
//
// The grid is `auto-fill`, and no card needs artwork drawn for it: a feature
// added to trove/features.py appears here complete, with its name, its tagline,
// its detail and its count, without anything being drawn or laid out for it.
// That is what makes this a container rather than eight cards.
//
// A sheet over the archive rather than a screen instead of it, so that saving
// puts you back on the Library health panel while the chain is growing its new
// link -- and so it never reads as having left the archive at all.
//
// Nothing is applied until Save: one POST, to the same /api/archive/configure
// the setup screen posts to, so a change of mind costs nothing and the download
// figure in the footer is one you can still refuse.

import {
  canAdd, canRemove, cost, costClass, lonelyPair, pendingDownloadMb,
} from "./feature-rules.js";
import {
  jget, jpost,
} from "./api.js";
import {
  esc, toast,
} from "./dom.js";
import {
  featureDocsLink,
} from "./docs.js";
import {
  renderNav, resetSectionViews, showSection,
} from "./router.js";
import {
  ICONS, S, archiveSections,
} from "./state.js";

// `was` is what the server runs, `chosen` is what this visit would have it run.
// Keeping both is what lets the sheet tell a fact from a plan: a card reports
// against `was`, the footer totals `chosen`, and Save is inert until they
// disagree. Nothing here records which card is showing its description: that
// follows the pointer and is only ever CSS, so re-rendering the sheet cannot
// take away what someone is reading.
const SHEET = {
  catalogue: [], chosen: new Set(), was: new Set(),
  text: null, busy: false,
};

// The feature's mark, resolved against the same ICONS the nav, the setup cards
// and the health rows draw from. The mark on the card you switch off here is
// the mark on the health row that stops reporting.
function mark(f) { return `<i class="feat-mark" aria-hidden="true">${ICONS[f.icon] || ""}</i>`; }

function feature(id) { return SHEET.catalogue.find(f => f.id === id); }

export function featureSheetOpen() {
  return document.getElementById("features-sheet").classList.contains("open");
}

export async function openFeatureSheet() {
  if (!S.arch) return;
  // Both fetched on every open rather than cached for the session. `ready` is
  // the one catalogue field that goes stale behind your back -- a download
  // finishing while the app is open flips it -- and it is the field the
  // footer's figure is built from. The text count the Overview holds is read
  // again for the same reason: it climbs while the pass runs, and this is the
  // screen where someone decides whether to let it finish.
  const [answer, text] = await Promise.all([
    jget("/api/features").catch(() => null),
    jget("/api/browse/text/status?root=" + S.arch.id).catch(() => null),
  ]);
  if (!answer || !answer.features) { toast("Couldn’t read the feature list.", true); return; }
  SHEET.catalogue = answer.features;
  SHEET.text = text;
  SHEET.was = new Set(S.arch.features || SHEET.catalogue.map(f => f.id));
  SHEET.chosen = new Set(SHEET.was);
  SHEET.busy = false;
  renderSheet();
  setSheetVisible(true);
  document.getElementById("features-sheet").focus();
}

export function closeFeatureSheet() { setSheetVisible(false); }

// docs.js hides and restores the sheet the same way, through the DOM: opening a
// feature's page must not leave the sheet floating over it, and coming back
// must not have discarded what was switched. It does it there rather than
// calling this, because this module imports docs.js and the reverse would be a
// cycle -- the same reason the setup screen is handled from there by id.
function setSheetVisible(on) {
  const sheet = document.getElementById("features-sheet");
  const backdrop = document.getElementById("features-backdrop");
  sheet.classList.toggle("open", on);
  backdrop.classList.toggle("open", on);
  sheet.setAttribute("aria-hidden", on ? "false" : "true");
}

// ---- the fact -------------------------------------------------------

function counted(n, one, many, none) {
  if (!n) return none;
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

// What this feature is to this archive, in one line.
//
// Read off the summaries the Overview already holds, so the sheet quotes the
// same numbers as the panel it was opened from rather than a second opinion.
//
// Deliberately fixed to what the *server* runs, not to the switch beside it: a
// card's fact does not change as its switch is flipped, because the fact has
// changed. Turning People off leaves "1,204 faces" standing, which is precisely
// what is being set aside -- and the footer says it is kept.
function fact(f) {
  if (!SHEET.was.has(f.id)) return cost(f).text;
  const s = S.summary, ds = S.dupsum, fs = S.facesum, ps = S.petsum, ss = S.semanticsum;
  switch (f.id) {
    case "index": return counted(s?.total, "file catalogued", "files catalogued",
      "Nothing catalogued yet");
    case "duplicates": return counted(ds?.duplicates, "redundant copy", "redundant copies",
      "No redundant copies");
    case "people": return counted(fs?.faces, "face", "faces", "No faces found");
    case "pets": return counted(ps?.detections, "animal", "animals", "No animals found");
    case "places": return counted(s?.in_places, "photo placed", "photos placed",
      "No places found");
    // "items" was the only place in the app that called a file something else.
    case "semantic": return counted(ss?.indexed, "file indexed", "files indexed",
      "Nothing indexed yet");
    // Both text halves fill one index from one pass, so there is no per-half
    // count to quote and the same figure is the honest answer on both cards: it
    // is what the archive can search, which is what either half was switched on
    // for.
    case "documents":
    case "ocr": return counted(SHEET.text?.read, "file read", "files read", "Nothing read yet");
    default: return "";
  }
}

// ---- rendering -------------------------------------------------------------

// Whether this card's switch can move at all. A required feature has no switch,
// and neither has one whose backend is not installed -- except when it is
// already running, since an install that loses an optional dependency between
// versions must not leave the archive with a stage it has no way to stop.
function canToggle(f) {
  return !f.required && (f.available || SHEET.was.has(f.id));
}

function control(f) {
  if (f.required) return `<span class="fcard-fixed">Always runs</span>`;
  if (!canToggle(f)) return `<span class="fcard-fixed">Not in this build</span>`;
  const on = SHEET.chosen.has(f.id);
  return `<button type="button" class="fsw" role="switch" aria-checked="${on}"
      aria-label="${esc(f.label)}" data-tip="${esc(f.label)}"
      onclick="event.stopPropagation();toggleSheetFeature('${f.id}')">
      <i aria-hidden="true"></i></button>`;
}

// The fact, in the slot the setup card gives its price -- and carrying that
// card's tone when it *is* a price, so "300 MB", "Downloaded" and "No download
// needed" are the same three colours here as there.
//
// A running feature's fact takes no tone at all. It is a count of what this
// archive got, not one of the three answers about what it would cost, and
// borrowing a colour that means "already paid" to say "1,204 faces" would be
// two different facts wearing one uniform.
function factCell(f) {
  const running = SHEET.was.has(f.id);
  return `<span class="${running ? "set-cost" : costClass(f)}">${esc(fact(f))}</span>`;
}

// The setup card, with a switch where its Add pill goes and this archive's own
// numbers where its price goes. Same classes, so the two catalogues cannot
// quietly become two different-looking products; `.fcard` carries only what
// differs, and every rule it adds is in features.css.
//
// No cover art, unlike its twin. The drawings there sell four features to
// someone who has never seen what they produce; here the fact under the name is
// the real thing, for every feature, including the ones nobody has drawn a
// preview for -- which is the difference between a catalogue that grows by
// itself and one that needs artwork commissioned before it can list anything.
function card(f) {
  const on = SHEET.chosen.has(f.id);
  const name = `<span class="set-card-name">${mark(f)}${esc(f.label)}</span>`;
  // The fact and the switch, on both faces. The description covers the card
  // while the pointer rests on it, so a foot printed only on the front would
  // mean that reading what a feature does takes the switch for it away.
  const foot = `<div class="set-card-foot">${factCell(f)}${control(f)}</div>`;
  // The whole card switches it -- a switch 40px wide is a small target for a
  // decision this size -- and BOTH faces do, not just the front. The
  // description covers the card, so a handler on the front alone means the card
  // stops being a switch the moment you point at it, which is every moment you
  // might press it. The two controls that are not this decision stop the click
  // before it reaches here: the switch would fire twice and land back where it
  // started, and "How it works" would toggle the feature it explains.
  const face = canToggle(f) ? ` onclick="toggleSheetFeature('${f.id}')"` : "";
  // The accent ring means "on because you chose it". Required features are on
  // by definition, and giving them the same ring left all eight cards outlined
  // identically -- a mark that distinguished nothing, next to a switch that
  // already carries the state.
  const chosenByUser = on && canToggle(f);
  return `<li class="set-card fcard${chosenByUser ? " on" : ""}${canToggle(f) ? "" : " fcard-fixed-face"}"
      data-feature="${f.id}">
      <div class="set-face"${face}>
        <div class="set-meta">
          ${name}
          <p class="set-card-line">${esc(f.tagline)}</p>
          ${foot}
        </div>
      </div>
      <div class="set-face set-back"${face}>
        ${name}
        <p class="set-card-detail">${esc(f.detail)}</p>
        <div class="set-back-foot">${featureDocsLink(f.id)}</div>
        ${foot}
      </div>
    </li>`;
}

// In catalogue order, which is pipeline order -- the order of the chain on the
// health panel this was opened from. Grouping the off ones at the end would
// read as a tidier catalogue and would be a different archive's story.
function renderSheet() {
  document.getElementById("fsheet-body").innerHTML =
    `<ul class="set-cards fcards">${SHEET.catalogue.map(card).join("")}</ul>`;
  syncFoot();
}

export function toggleSheetFeature(id) {
  const f = feature(id);
  if (!f) return;
  if (!canToggle(f)) return;
  if (SHEET.chosen.has(id)) {
    if (!canRemove(f, SHEET.chosen)) return;
    SHEET.chosen.delete(id);
  } else {
    // The second half is the switch `canToggle` left on an unavailable feature
    // that was already running: having offered the way out, this has to be the
    // way back in, or the switch is one-directional and pressing it twice is a
    // trap rather than a change of mind.
    if (!canAdd(f, SHEET.chosen) && !SHEET.was.has(id)) return;
    SHEET.chosen.add(id);
  }
  syncCard(id);
  syncFoot();
}

// Patched, not re-rendered. Rebuilding the grid would throw away the focus of
// the switch just pressed -- which for anyone driving this from the keyboard is
// the screen going out from under them mid-decision -- and would turn back
// every card someone had opened to read.
function syncCard(id) {
  const el = document.querySelector(`.fcard[data-feature="${id}"]`);
  if (!el) return;
  const on = SHEET.chosen.has(id);
  // Same rule as the initial render: the ring is for a feature the user chose.
  const feature = SHEET.catalogue.find(f => f.id === id);
  el.classList.toggle("on", on && !!feature && canToggle(feature));
  const sw = el.querySelector(".fsw");
  if (sw) sw.setAttribute("aria-checked", String(on));
}

function setNote(id, text) {
  const el = document.getElementById(id);
  el.textContent = text || "";
  el.hidden = !text;
}

function changed() {
  return SHEET.chosen.size !== SHEET.was.size
    || [...SHEET.chosen].some(id => !SHEET.was.has(id));
}

function syncFoot() {
  /* Whose cost this is.

     The figure is what the *selection* still owes, which is real but was
     labelled "715 MB to download" beside a disabled Save button, on a sheet
     where nothing had been changed -- so it read as the price of saving. It
     says who owes it now: a change quotes what the change adds, and an
     untouched sheet reports what the archive is already waiting on. */
  const now = pendingDownloadMb(SHEET.catalogue, SHEET.chosen);
  const before = pendingDownloadMb(SHEET.catalogue, SHEET.was);
  const added = now - before;
  setNote("fsheet-total", changed()
    ? (added > 0 ? `This change adds ${added} MB to download` : "")
    : (now ? `${now} MB still to download` : ""));

  const pair = lonelyPair(SHEET.catalogue, SHEET.chosen);
  setNote("fsheet-pair", pair && `${pair[0].label} would run without ${pair[1].label}. `
    + `The two check each other's work, so having both makes each of them more accurate.`);

  // Shown only once something has actually been switched off. A standing
  // reassurance is a notice nobody reads, and until a switch goes off there is
  // nothing to reassure anyone about.
  const dropped = [...SHEET.was].some(id => !SHEET.chosen.has(id));
  setNote("fsheet-kept", dropped && "Turning a feature off keeps everything it found. "
    + "Switch it back on and it picks up where it left off.");

  const save = document.getElementById("fsheet-save");
  save.disabled = SHEET.busy || !changed();
}

// ---- saving ----------------------------------------------------------------

export async function saveFeatureSheet() {
  if (SHEET.busy || !S.arch || !changed()) return;
  SHEET.busy = true;
  syncFoot();
  const features = SHEET.catalogue.filter(f => SHEET.chosen.has(f.id)).map(f => f.id);
  const result = await jpost("/api/archive/configure", { root_id: S.arch.id, features })
    .catch(() => ({ error: "Couldn’t save what this archive runs." }));
  SHEET.busy = false;
  if (!result || result.error) {
    toast((result && result.error) || "Couldn’t save what this archive runs.", true);
    syncFoot();
    return;
  }
  // The server has the last word on the set -- the two required features are
  // added back whatever was posted -- so take its answer rather than the
  // request. Everything downstream reads this: the nav, the Overview's tiles,
  // and which search ways Browse offers.
  S.arch.features = result.features || features;
  closeFeatureSheet();
  // A feature is a structural change to the archive, not a preference: sections
  // appear and disappear, and the Overview's tiles and health chain are built
  // from the set. Drop every stashed section so each is rebuilt from the new
  // answer instead of resumed from a fragment that predates it.
  resetSectionViews();
  renderNav();
  const live = archiveSections(S.arch);
  showSection(live.some(s => s.id === S.section) ? S.section : "overview");
}
