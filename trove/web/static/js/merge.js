// Drag-to-merge, shared by the People, Pets and Places grids: the drag
// affordance itself, the name-choice dialog, the POST that performs the merge,
// and the "merged in" panel with its undo. Nothing here knows which of the
// three entity types it is working on beyond the label it was handed.

import {
  showPet,
} from "./pets.js";
import {
  showPerson,
} from "./people.js";
import {
  jget, jpost,
} from "./api.js";
import {
  esc, toast,
} from "./dom.js";
import {
  refreshPlacesAfterMerge,
} from "./places.js";
import {
  S,
} from "./state.js";

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
export function guardCardClick(fn) {
  return (...args) => { if (!MERGE_DROP_GUARD) fn(...args); };
}
export function attachMergeDrag(card, info, onMerged) {
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
      warning = `These photos span a wide area: some sit ${dist} km from the centre of the merged place.`;
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
export function mergeAskCancel() { if (_mergeAskResolve) _mergeAskResolve(null); }
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
export function mergesPanel(merges, kind) {
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
export async function undoMerge(mergeId, kind) {
  const url = kind === "pet" ? "/api/pets/unmerge"
    : kind === "place" ? "/api/map/cluster/unmerge" : "/api/faces/unmerge";
  const res = await jpost(url, { merge_id: mergeId });
  if (!res || res.error) { toast((res && res.error) || "Couldn’t undo that merge.", true); return; }
  // People/pets requeue a background recluster, so their toast says so; a place
  // merge is a direct row move/restore (places.py's unmerge_place_clusters),
  // nothing gets queued, so "Undone" alone is accurate here.
  toast(kind === "place" ? "Undone" : "Undone. Regrouping in the background…");
  if (kind === "pet") { if (S.currentPet) showPet(S.currentPet.id); }
  else if (kind === "place") { refreshPlacesAfterMerge(); }
  else if (S.facePerson != null) showPerson(S.facePerson);
}
