// Working on several groups at once: the selection itself, the bar that says
// what can be done with it, and the doing.
//
// Three screens draw grids of the same card -- People, Pets, Places -- and the
// things you do to a group are the same on all three: fold several into one,
// or take several off the screen. Doing them one card at a time is the whole
// cost of tidying an archive, because the answer is rarely about one card: a
// party leaves thirty strangers, and they are thirty of the same decision.
//
// So this module owns the mode and the bar, and each screen tells it three
// things -- which grid, which words, and what to do afterwards. What a screen
// must NOT tell it is how to merge or hide: those are the same endpoints the
// single-card menu already posts to, driven here in a loop, so a bulk action
// and a one-off can never come to mean different things.

import {
  qpost,
} from "./api.js";
import {
  esc, toast,
} from "./dom.js";
import {
  askConfirm, askMergeName,
} from "./merge.js";

/* The one selection on screen, or null. Keyed by kind rather than by screen so
   a stale bar from People cannot act on the Pets grid: leaving a section drops
   the mode (endSelecting, called by the router), and every read below is
   already scoped to the kind that asked. */
let MODE = null;   // { kind, chosen: Map<id, {id,name,photos}>, gridId, after }

export function selecting(kind) { return !!MODE && MODE.kind === kind; }

export function endSelecting() {
  if (!MODE) return;
  const { gridId, kind } = MODE;
  MODE = null;
  paint(gridId);
  const bar = document.getElementById("selectbar");
  if (bar) bar.remove();
  syncSelectButton(kind);
}

/* Enter selection, or leave it. The screen's own button calls this; it does not
   own the state, so the button can be redrawn by any refresh without the
   selection being lost with it. */
export function toggleSelecting(kind, gridId, after) {
  if (selecting(kind)) { endSelecting(); return; }
  endSelecting();
  MODE = { kind, chosen: new Map(), gridId, after };
  paint(gridId);
  renderBar();
  syncSelectButton(kind);
}

/* What a card's click means now.

   The card still opens the group when nothing is being selected, which is why
   every grid routes its open through here rather than checking the mode itself
   -- one place decides, and a screen cannot forget to ask. */
export function openOrSelect(kind, item, open) {
  if (!selecting(kind)) { open(); return; }
  if (MODE.chosen.has(item.id)) MODE.chosen.delete(item.id);
  // What the card says about itself, kept rather than looked up again: merging
  // several named groups has to ask which name survives, and by the time it
  // asks, the cards it is asking about may have been repainted by a poll.
  else MODE.chosen.set(item.id, { id: item.id, name: item.name, photos: item.photos || 0 });
  paint(MODE.gridId);
  renderBar();
}

// Which cards are marked, read back off the grid on every change. Cheap enough
// -- these grids hold a few hundred cards at most -- and it means a card the
// infinite list has only just added is marked correctly without anything
// having to notice that it arrived.
function paint(gridId) {
  const grid = document.getElementById(gridId); if (!grid) return;
  grid.classList.toggle("selecting", !!MODE);
  [...grid.children].forEach(card => {
    const id = Number(card.dataset.syncKey);
    card.classList.toggle("is-selected", !!MODE && MODE.chosen.has(id));
  });
}

/* The words each kind uses for the same three acts.

   Written out rather than composed, because "Not a person" and "Not an animal"
   are different claims about different things, and a screen that says one of
   them by substituting a noun into the other is a screen that will eventually
   say "Not a place". Places genuinely have only the one action: there is
   nothing a place can be instead of a place. */
const KINDS = {
  person: {
    noun: n => `${n} ${n === 1 ? "person" : "people"}`, plural: "people",
    merge: "/api/faces/merge", mergeKey: "person",
    hide: "/api/faces/hide", hideKey: "person_id",
    reject: "Not people", rejectReason: "not_person",
    rejectTitle: n => `Not people? (${n} ${n === 1 ? "group" : "groups"})`,
    rejectAsk: n => `The faces in ${n} ${n === 1 ? "group" : "groups"} are marked as dolls, `
      + `animals or cartoons and left out of grouping from now on. Nothing is deleted.`,
    rejectDo: "Not people",
    unknown: "Unknown people",
  },
  pet: {
    noun: n => `${n} ${n === 1 ? "pet" : "pets"}`, plural: "pets",
    merge: "/api/pets/merge", mergeKey: "pet",
    hide: "/api/pet/hide", hideKey: "pet_id",
    reject: "Not animals", rejectReason: "not_animal",
    rejectTitle: n => `Not animals? (${n} ${n === 1 ? "group" : "groups"})`,
    rejectAsk: n => `The photos in ${n} ${n === 1 ? "group" : "groups"} are left out of pet `
      + `grouping from now on. Nothing is deleted.`,
    rejectDo: "Not animals",
    unknown: "Unknown animals",
  },
  place: {
    noun: n => `${n} ${n === 1 ? "place" : "places"}`, plural: "places",
    merge: "/api/map/cluster/merge", mergeKey: "place",
  },
};

/* The bar, drawn while something is selected and gone when nothing is.

   Fixed to the foot of the window rather than sitting in the page: the cards
   being chosen are spread down a grid several screens long, and a bar that
   scrolls away is one you have to scroll back to before you can act on what
   you just chose. */
function renderBar() {
  const words = KINDS[MODE.kind];
  const n = MODE.chosen.size;
  let bar = document.getElementById("selectbar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "selectbar";
    bar.className = "selectbar";
    document.body.appendChild(bar);
  }
  const act = (label, name, enabled, danger) =>
    `<button type="button" class="quietbtn selbtn${danger ? " is-danger" : ""}" data-act="${name}"${
      enabled ? "" : " disabled"}>${esc(label)}</button>`;
  bar.innerHTML = `<span class="selcount">${n ? esc(words.noun(n)) : "Nothing selected"}</span>`
    // Merging needs two: one group folded into itself is not a merge, and a
    // button that says otherwise has to be pressed to find that out.
    + act("Merge", "merge", n >= 2)
    + (words.unknown ? act(words.unknown, "unknown", n >= 1) : "")
    + (words.reject ? act(words.reject, "reject", n >= 1, true) : "")
    + `<button type="button" class="quietbtn selbtn seldone" data-act="done">Done</button>`;
  bar.querySelectorAll("button[data-act]").forEach(button => {
    button.onclick = () => runAction(button.dataset.act);
  });
}

async function runAction(name) {
  if (name === "done") { endSelecting(); return; }
  const words = KINDS[MODE.kind], chosen = [...MODE.chosen.values()], after = MODE.after;
  if (name === "reject" && !await askConfirm({
    title: words.rejectTitle(chosen.length), body: words.rejectAsk(chosen.length),
    confirmLabel: words.rejectDo, danger: true,
  })) return;
  let failed;
  if (name === "merge") {
    const survivingName = await chooseSurvivingName(words, chosen);
    if (survivingName === null) return;    // cancelled: nothing chosen, nothing done
    failed = await mergeAll(words, chosen.map(c => c.id), survivingName);
  } else {
    failed = await hideAll(words, chosen.map(c => c.id),
      name === "reject" ? words.rejectReason : "unknown");
  }
  endSelecting();
  if (failed) toast(failed, true);
  after();
}

/* Which name the merged group keeps, asked once for the whole set.

   The backend refuses to merge two groups that are both named -- there is no
   automatic way to choose between two things a person typed -- so a set holding
   more than one name has to settle it before any of the merges run, or the
   first pair would stop the whole thing. The same dialog a drag-merge uses, so
   the question is asked in the same words wherever it comes up.

   Returns "" when there is nothing to settle (no names, or one name shared),
   and null when the dialog was cancelled -- which must not read as "no name". */
async function chooseSurvivingName(words, chosen) {
  const named = chosen.filter(c => (c.name || "").trim());
  const names = [...new Set(named.map(c => c.name.trim()))];
  if (names.length < 2) return "";
  return askMergeName({
    title: `Merge these ${chosen.length} ${words.plural}?`,
    body: "They have different names. Which one should stay?",
    options: named.map(c => ({ value: c.name.trim(), label: c.name.trim(), count: c.photos })),
    preselect: names[0],
  });
}
/* Fold the chosen groups into one, by folding them in one at a time.

   Sequential and through the same endpoint a single merge uses, rather than a
   bulk one of its own: merging is the operation here with the most rules --
   which name survives, which id does, the durable constraint that keeps the
   merge through the next re-clustering -- and a second implementation of it
   would be a second set of answers to all of them.

   Each merge returns the survivor, which is what the next one folds into. So a
   name chosen anywhere in the set carries through to the end, and the caller
   never has to guess which of the chosen groups will be the one left standing.
*/
async function mergeAll(words, ids, name) {
  let survivor = ids[0];
  for (const id of ids.slice(1)) {
    let res;
    const body = { a: survivor, b: id };
    if (name) body.name = name;
    try { res = await qpost(words.merge, body); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) {
      return `Merged what it could. ${(res && res.error) || "One of them would not merge."}`;
    }
    survivor = (res[words.mergeKey] || {}).id || survivor;
  }
  return "";
}

async function hideAll(words, ids, reason) {
  let stopped = "";
  for (const id of ids) {
    let res;
    try { res = await qpost(words.hide, { [words.hideKey]: id, reason }); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) stopped = (res && res.error) || "Some of them would not move.";
  }
  return stopped;
}

/* What each screen's grid is called and what to do once something has been
   done to it. Registered when the screen renders rather than passed through the
   button, because the button is markup in a page head and the answer is a
   function -- and because a screen that has been re-rendered has a new grid to
   point at and the same answer.

   The alternative is for this module to know the three screens by name, which
   is the wrong way round: they know about selection, selection does not know
   about them. */
const REGISTERED = {};
export function selectable(kind, gridId, after) { REGISTERED[kind] = { gridId, after }; }

/* The control that starts it, for a screen's page head, and the handler behind
   it. Markup with an inline handler like every other control these screens
   build, so tools/dev/check_handlers.py can see what it calls.

   Starts hidden, because the grid it selects from is filled after the head is
   drawn: on People and on Pets this button sat at full strength directly above
   "No faces yet" and "No repeated pets grouped yet", offering to enter a
   selection mode over nothing. `syncSelectButton` reveals it once the grid has
   a first card, and takes it away again if the last one goes. */
export function selectButton(kind, label = "Select") {
  return `<button type="button" class="quietbtn selectstart" data-select-kind="${kind}"
    aria-pressed="false" hidden onclick="startSelecting('${kind}')">${esc(label)}</button>`;
}
/* Show the Select control for `kind` only when its grid holds something.

   Asked of the cards rather than of `data-sync-key`: People and Pets set that
   key on every card they build, and Places does not, so keying off it would
   leave the Places button permanently hidden. What every grid does share is
   that its empty state is a `.muted` sentence and its cards are not. */
export function syncSelectButton(kind) {
  const button = document.querySelector(`.selectstart[data-select-kind="${kind}"]`);
  const screen = REGISTERED[kind];
  if (!button || !screen) return;
  const grid = document.getElementById(screen.gridId);
  const cards = grid ? [...grid.children].filter(el => !el.classList.contains("muted")) : [];
  button.hidden = !cards.length;
  // Lit while the mode is on. This is a toggle -- pressing it again leaves
  // selection -- and it used to be pixel-identical in both states, so the only
  // evidence the mode was on at all was the bar at the foot of the window. Which
  // left the button saying "Select" over a screen that was already selecting,
  // and cancelling for anyone who pressed it expecting to start.
  button.setAttribute("aria-pressed", selecting(kind) ? "true" : "false");
}
export function startSelecting(kind) {
  const screen = REGISTERED[kind];
  if (screen) toggleSelecting(kind, screen.gridId, screen.after);
}
