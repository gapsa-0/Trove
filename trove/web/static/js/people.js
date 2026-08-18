// The People screen: the face-detection status panel and its poll, the people
// grid and its incremental resync, the "same person?" suggestion review, and
// the single-person page. Merging is drag-to-merge's job, not this module's.

import {
  detectStatusRow, syncCardGrid, thumbCollage,
} from "./cards.js";
import {
  renderNav,
} from "./router.js";
import {
  personTile,
} from "./library.js";
import {
  galleryFromGrid,
} from "./gallery.js";
import {
  startInfiniteList,
} from "./infinite.js";
import {
  jget, jpost, oneAtATime,
} from "./api.js";
import {
  docsButton,
} from "./docs.js";
import {
  setStat, why,
} from "./statwhy.js";
import {
  esc, fileCount, toast,
} from "./dom.js";
import {
  historyButton, mountHistory,
} from "./history.js";
import {
  cardMenu,
} from "./menu.js";
import {
  attachMergeDrag, guardCardClick, mergeWithPicker,
} from "./merge.js";
import {
  inlineNameEdit,
} from "./nameedit.js";
import {
  onSnapshot,
} from "./pipeline.js";
import {
  S,
} from "./state.js";

// "800 / 1,204": what has been looked at, over what there is to look at. One
// function because it is written twice -- into the tile at render, and into
// both copies of it on every poll.
function scannedFigure(sum) {
  return `${sum.scanned.toLocaleString()} <small>/ ${sum.total_images.toLocaleString()}</small>`;
}

export async function renderFaces(m) {
  const gen = S.nav, root = S.arch.id;
  S.facePerson = null;
  STASHED_PEOPLE = null;   // a full render replaces whatever was set aside
  const sum = await jget("/api/faces/summary?root=" + root);
  if (gen !== S.nav) return;
  if (!sum.backend_available) {
    m.innerHTML = `<div class="pagehead"><div><h2 class="sec">People</h2><p>Find familiar faces and organize them with names.</p></div>${docsButton("people")}</div>
      <div class="panel"><div class="d pending"><span class="dot pending"></span>Face detection needs OpenCV's DNN face module.</div>
      <p class="muted">Install a modern <code>opencv-python</code> (the <code>media</code> extra) and reopen this tab.</p></div>`;
    return;
  }
  S.faceSum = sum;
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">People</h2><p>Find familiar faces, review matches, and add names without leaving this page.</p></div>${docsButton("people")}</div>
    <div class="statrow">
      <div class="stat"><div><div class="k">People</div><div class="v" id="fs-people">${sum.people.toLocaleString()}</div></div>
        ${why("People", sum.people.toLocaleString(), "Groups of faces taken to be the same person, named or not yet named.")}</div>
      <div class="stat"><div><div class="k">Faces</div><div class="v" id="fs-faces">${sum.faces.toLocaleString()}</div></div>
        ${why("Faces", sum.faces.toLocaleString(), "Every face found, before grouping. Three people in one photo is three.")}</div>
      <div class="stat"><div><div class="k">Photos with faces</div><div class="v" id="fs-photos">${sum.photos_with_faces.toLocaleString()}</div></div>
        ${why("Photos with faces", sum.photos_with_faces.toLocaleString(), "Photos holding at least one face, however many that photo holds.")}</div>
      <div class="stat"><div><div class="k">Scanned</div><div class="v" id="fs-scanned">${scannedFigure(sum)}</div></div>
        ${why("Scanned", scannedFigure(sum), "Photos face detection has looked at, of all the photos it will look at.")}</div>
    </div>
    <div class="panel" id="facejob"></div>
    <div id="peoplewrap"><div class="muted" style="padding:20px">Loading people…</div></div>`;
  renderFaceStatus();
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
//
// Reads both halves out of state rather than taking them as arguments, because
// they now arrive separately: the summary from this screen's own poll, the
// detect stage from the shared snapshot. Either can land first, and either
// should redraw the row with whatever the other one last said.
function renderFaceStatus() {
  const el = document.getElementById("facejob"); if (!el) return;
  const stage = detectStage();
  // Keep a failed attempt visible during the scheduler's retry cooldown
  // instead of making the progress panel blink.
  const failed = stage && stage.state === "error" && S.faceSum && S.faceSum.unscanned > 0
    ? (stage.message || "The face worker stopped before reporting progress.") : null;
  el.innerHTML = detectStatusRow(S.faceSum, failed);
}
// People and Pets both report on the one fused `detect` stage.
const detectStage = () => ((S.pipeline && S.pipeline.stages) || []).find(s => s.id === "detect");
const detectProgress = () => {
  const stage = detectStage();
  return stage && stage.state === "running" ? stage.progress : null;
};
export function startFacePoll() { stopFacePoll(); facePoll = setInterval(faceTick, 1500); faceTick(); }
export function stopFacePoll() { if (facePoll) { clearInterval(facePoll); facePoll = null; } }
let facePoll = null;
// The pipeline snapshot arrives from the one poller rather than being fetched
// here. This tick used to ask for it alongside the summary -- two requests every
// 1.5s for a thing the sidebar chip already had -- which on a slow snapshot was
// the fastest way in the app to spend the browser's ~6 connections and leave the
// grid this poll exists to refresh unable to load at all.
onSnapshot(() => renderFaceStatus());
// Live refresh while a faces job runs: the stat tiles tick every poll, and
// the people grid is *patched* (syncPeopleGrid) rather than rebuilt, so the
// page never resets under the user -- scroll position, the pages the
// infinite list has already loaded and the "Same person?" review queue all
// survive, and only cards whose data actually changed are touched.
const faceTick = oneAtATime(async () => {
  const area = document.getElementById("facejob"); if (!area) { stopFacePoll(); return; }
  const sum = await jget("/api/faces/summary?root=" + S.arch.id);
  const fj = detectProgress();
  const wasRunning = S.faceJobRunning; S.faceJobRunning = !!fj;
  const prev = S.faceSum || {}; S.faceSum = sum;
  setStat("fs-people", sum.people.toLocaleString());
  setStat("fs-faces", sum.faces.toLocaleString());
  setStat("fs-photos", sum.photos_with_faces.toLocaleString());
  setStat("fs-scanned", scannedFigure(sum));
  renderFaceStatus();
  if (fj) {
    if (sum.people !== prev.people || sum.faces !== prev.faces) syncPeopleGrid();
  } else if (wasRunning) {
    syncPeopleGrid();   // final pass finished → reconcile once more
  }
});
const PEOPLE_PAGE_SIZE = 120;
async function fetchPeoplePage(offset) {
  const res = await jget(`/api/faces/persons?root=${S.arch.id}&offset=${offset}&limit=${PEOPLE_PAGE_SIZE}`);
  return res.people;
}
async function renderPeople() {
  const wrap = document.getElementById("peoplewrap"); if (!wrap) return;
  wrap.innerHTML = `<div id="suggestwrap"></div><div class="people" id="peoplegrid"></div>
    <div class="infinite-status" id="people-sentinel" aria-live="polite"></div>
    <div id="hiddenwrap"></div>`;
  renderHidden();
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
/* The groups taken off the screen, kept reachable at the foot of it.

   Below the grid and collapsed, because it is a place you go back to rather
   than something to look at: hidden is what you asked for. It carries its own
   count so the section can say how much is behind it without being opened, and
   it is absent entirely when there is nothing hidden -- an empty drawer
   advertising itself is how the merge panel went wrong. */
const HIDDEN_PAGE_SIZE = 120;
async function renderHidden() {
  const wrap = document.getElementById("hiddenwrap"); if (!wrap) return;
  const n = (S.faceSum && S.faceSum.hidden_people) || 0;
  const previous = document.querySelector(".hidden-people");
  // Restoring one group should not shut the drawer you are working through.
  const wasOpen = !!previous && previous.hasAttribute("open");
  if (!n) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<details class="hidden-people"${wasOpen ? " open" : ""}>
      <summary>Hidden <span class="muted">· ${n.toLocaleString()} group${n === 1 ? "" : "s"}</span></summary>
      <div class="people" id="hiddengrid"></div>
      <div class="infinite-status" id="hidden-sentinel" aria-live="polite"></div>
    </details>`;
  startInfiniteList("hiddenPeopleList", {
    sentinelId: "hidden-sentinel", pageSize: HIDDEN_PAGE_SIZE,
    fetchPage: async offset => {
      const res = await jget(`/api/faces/persons?root=${S.arch.id}&hidden=1&offset=${offset}&limit=${HIDDEN_PAGE_SIZE}`);
      return res.people;
    },
    onPage: (people, { first }) => {
      const grid = document.getElementById("hiddengrid"); if (!grid) return;
      if (first) grid.innerHTML = "";
      people.forEach(p => grid.appendChild(hiddenCard(p)));
    },
  });
}
function hiddenCard(p) {
  const d = document.createElement("div"); d.className = "pcard is-hidden";
  d.dataset.syncKey = String(p.id);
  d.innerHTML = faceCollage(personCoverIds(p)) + `<div class="pmeta">
      <div class="pname ${p.name ? "" : "un"}">${p.name ? esc(p.name) : "Unnamed group"}</div>
      <div class="pcount">${fileCount(p.photos)}</div>
      <button class="linkbtn" type="button">Put back</button></div>`;
  d.querySelector("button").onclick = e => { e.stopPropagation(); unhidePerson(p.id); };
  return d;
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
  await patchPeopleGrid();
}
/* The same patch, run because the user just changed something rather than
   because the clock came round.

   It skips the poll's guards on purpose. Merging used to rebuild the grid from
   the first page, which on a screen with hundreds of clusters threw away the
   scroll position and every page loaded into it -- and merging is the one
   thing here you do dozens of times in a row. It does not skip the fetch: an
   optimistic removal would be undone by whichever poll had already asked the
   server before the merge landed. */
export async function refreshPeopleGrid() {
  if (!document.getElementById("peoplegrid")) return;
  await patchPeopleGrid();
}
async function patchPeopleGrid() {
  const st = S.peopleList; if (!st) return;
  const limit = Math.min(PEOPLE_SYNC_LIMIT, Math.max(PEOPLE_PAGE_SIZE, st.offset));
  S.peopleSyncing = true;
  let people;
  try { people = (await jget(`/api/faces/persons?root=${S.arch.id}&offset=0&limit=${limit}`)).people; }
  catch { return; }
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
export async function answerSuggest(kind) {
  const st = S.suggest, s = st.list && st.list[st.idx]; if (!s) return;
  // drop any later queued pair that references a now-removed cluster
  const dropRefs = ids => { st.list = st.list.filter((x, ix) => ix <= st.idx || (!ids.includes(x.a.id) && !ids.includes(x.b.id))); };
  if (kind === 'same') {
    const res = await jpost('/api/faces/merge', { a: s.a.id, b: s.b.id });
    if (res && res.error) { alert(res.error); return; }
    if (res && res.person) { const kept = res.person.id, dropped = (s.a.id === kept ? s.b.id : s.a.id); dropRefs([dropped]); refreshPeopleGrid(); }
  } else if (kind === 'different') {
    await jpost('/api/faces/different', { a: s.a.id, b: s.b.id });
  } else if (kind === 'skip') {
    await jpost('/api/faces/skip', { a: s.a.id, b: s.b.id });
  } else if (kind === 'notpeople') {
    // The queue's own wording already says what these are ("dolls / pets /
    // cartoons"), so it does not ask a second time.
    await jpost('/api/faces/hide', { person_id: s.a.id, reason: 'not_person' });
    await jpost('/api/faces/hide', { person_id: s.b.id, reason: 'not_person' });
    dropRefs([s.a.id, s.b.id]); refreshPeopleGrid();
  }
  if (st.total > 0) st.total--;
  st.idx++;
  if (st.idx >= st.list.length) { renderPeople(); return; }  // reload grid + fresh queue
  renderSuggest();
}
/* The two ways a cluster leaves the People screen, as menu items.

   They are different claims and are worded as such. "Not a person" is about
   the detections -- a doll, a statue, a face on a poster -- and takes them out
   of clustering for good, so it asks first. "Unknown" is about the list: a
   real person whose faces go on clustering exactly as before, reversible from
   the Hidden section, and so nothing to confirm.

   `after` is what to do once it lands: the grid patches itself in place, a
   person's own page has nowhere left to be and goes back to the grid. */
function clusterMenuItems(p, after, onMerged) {
  return [
    {
      label: "Merge with…",
      submenu: (panel, close) => mergeWithPicker(
        panel, close, { kind: "person", id: p.id, name: p.name, photos: p.photos || 0 }, onMerged),
    },
    ...hideMenuItems(p.id, after),
  ];
}
function hideMenuItems(id, after) {
  return [
    {
      label: "Not a person",
      danger: true,
      onPick: async () => {
        if (!confirm("Not a person? Its faces are marked as a doll, animal or cartoon and left out of clustering from now on.")) return;
        await hideCluster(id, "not_person", after);
      },
    },
    { label: "Unknown person", onPick: () => hideCluster(id, "unknown", after) },
  ];
}
async function hideCluster(id, reason, after) {
  let res;
  try { res = await jpost("/api/faces/hide", { person_id: id, reason }); }
  catch (e) { res = { error: String(e) }; }
  if (!res || res.error) { toast("Couldn’t hide this group.", true); return; }
  if (reason === "unknown") toast("Hidden. Find it under “Hidden” to put it back.");
  after();
}
export async function unhidePerson(id) {
  let res;
  try { res = await jpost("/api/faces/unhide", { person_id: id }); }
  catch (e) { res = { error: String(e) }; }
  if (!res || res.error) { toast("Couldn’t restore this group.", true); return; }
  refreshAfterHide();
}
/* Both sides of the grid, after a group has crossed between them.

   The counts have to be re-read rather than adjusted by one: `hidden_people`
   is what decides whether the Hidden section exists at all, and the poll only
   refreshes the summary while a detection job is running -- so on a settled
   archive nothing else would ever tell this screen what just happened. */
async function refreshAfterHide() {
  await refreshPeopleGrid();
  const sum = await jget("/api/faces/summary?root=" + S.arch.id).catch(() => null);
  if (sum) {
    S.faceSum = sum;
    setStat("fs-people", sum.people.toLocaleString());
  }
  await renderHidden();
}
// Up to 4 faces as a 2x2 collage. `ids` is the person's faces_preview, with
// cover_face_id as fallback for old payloads. Shared with the Pets grid, which
// draws the same card from a different endpoint.
const faceCollage = ids => thumbCollage(ids, "/faceThumb");
// The preview face ids a card's collage is built from, as a string, so
// syncPeopleGrid can tell "same faces, new count" from "new faces".
function personCoverIds(p) {
  return (p.faces_preview && p.faces_preview.length ? p.faces_preview : [p.cover_face_id])
    .filter(Boolean).slice(0, 4);
}
function personMetaInner(p) {
  const nm = p.name ? esc(p.name) : "Name this person";
  // The text is its own column so the actions menu can sit beside it in the
  // space to its right, rather than over the photograph or under the count.
  return `<div class="pmeta-text">
    <button class="pname ${p.name ? "" : "un"}" type="button">${nm}</button>
    <div class="pcount">${fileCount(p.photos)}</div></div>`;
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
  // In the meta row, not over the photograph, and appended after the name so
  // it sits at the end of the line the count is on.
  cardMenu(d.querySelector(".pmeta"), clusterMenuItems(p, refreshAfterHide, refreshPeopleGrid));
  attachMergeDrag(d, d._merge, refreshPeopleGrid);
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
  // Rewriting the row drops the menu with it, so it goes back on.
  cardMenu(meta, clusterMenuItems(p, refreshAfterHide, refreshPeopleGrid));
  Object.assign(card._merge, { name: p.name, photos: p.photos });
  return true;
}
function editPersonCardName(card, p) {
  const meta = card.querySelector(".pmeta"); if (!meta) return;
  card.onclick = null;
  card.draggable = false;   // don't let an in-progress rename start a merge-drag
  meta.className = "pmeta pmeta-editing";
  inlineNameEdit(meta, {
    value: p.name,
    label: "Person’s name",
    after: `<div class="pcount">${fileCount(p.photos)} · Enter or click away to save</div>`,
    onSave: (name, input) => savePersonCardName(card, p, name, input),
    onCancel: () => card.replaceWith(personCard(p)),
  });
}
async function savePersonCardName(card, p, name, input) {
  if (name === (p.name || "")) { card.replaceWith(personCard(p)); return; }
  input.disabled = true;
  let result;
  try { result = await jpost("/api/faces/person/rename", { person_id: p.id, name }); }
  catch (e) { result = { error: String(e) }; }
  if (!result || result.error) {
    toast("Couldn’t save the person’s name.", true); card.replaceWith(personCard(p)); return;
  }
  // Out of editing state first, and with the new name already on it. The grid
  // patch that follows deliberately refuses to touch a card mid-rename
  // (updatePersonCard), so leaving this one in that state would leave the name
  // it was just given off the screen until the next poll.
  card.replaceWith(personCard({ ...p, name: name || null }));
  await refreshPeopleGrid();
}
const PERSON_PAGE_SIZE = 120;
/* Whose photos the arrows are walking, for the viewer's position readout.
   Held here because the detail screen already knows it and the viewer must not
   have to re-fetch a person to caption a counter. */
let PERSON_NAME = "";
function personGalleryLabel() {
  return PERSON_NAME ? `in ${PERSON_NAME}\u2019s photos` : "in this person\u2019s photos";
}
/* The grid, set aside while a person's page is open.
   Opening a person is not a section change, so the router's own stash does not
   cover it -- and rebuilding the grid on the way back drops the scroll position
   AND every page the infinite list had loaded, which on a screen of several
   hundred groups is most of what you were looking at. Same trick the router
   uses between sections: keep the nodes, put them back. */
let STASHED_PEOPLE = null;
function stashPeopleGrid(main) {
  if (STASHED_PEOPLE) return;
  const fragment = document.createDocumentFragment();
  const scrollTop = main.scrollTop;
  while (main.firstChild) fragment.appendChild(main.firstChild);
  STASHED_PEOPLE = { fragment, scrollTop };
}
export async function showPerson(id) {
  stopFacePoll();
  S.section = "people"; renderNav(); S.facePerson = id;
  if (S.arch) location.hash = `/archive/${S.arch.id}/people`;
  const m = document.getElementById("main");
  stashPeopleGrid(m);
  m.innerHTML = '<div class="muted" style="padding:30px">Loading…</div>';
  const r = await jget(`/api/faces/person/${id}?root=${S.arch.id}&limit=${PERSON_PAGE_SIZE}`);
  if (!r || r.error) { m.innerHTML = '<div class="soonbox">Person not found.</div>'; return; }
  PERSON_NAME = r.name || "";
  const nm = r.name ? esc(r.name) : "Name this person";
  const nmCls = r.name ? "nm" : "nm un";
  const safe = (r.name || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  // The cover, which is what "Make cover photo" sets. Falling back to the
  // first face on the page covers a person whose cover has not been derived
  // yet -- but taking that fallback *first*, as this did, meant the choice
  // survived in the database and nowhere on the screen.
  const avatarFace = r.cover_face_id || (r.items.find(it => it.face_id) || {}).face_id;
  const avatar = avatarFace
    ? `<img class="person-header-avatar" src="/faceThumb/${avatarFace}" alt="" onerror="this.style.visibility='hidden'">`
    : `<div class="person-header-avatar" aria-hidden="true"></div>`;
  m.innerHTML = `<div class="facetopbar">
      <button class="back back-control" type="button" onclick="backToPeople()" aria-label="Back to People" title="Back to People">
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
        <span class="muted ftb-count">${fileCount(r.photos)}</span>
      </div>
      ${historyButton("person", id, r.name)}
      <div class="ftb-actions" id="personactions"></div>
    </div>
    ${r.hidden ? `<div class="panel hidden-note">This group is hidden from People.
      <button class="linkbtn" type="button" onclick="unhidePerson(${id})">Put it back</button></div>` : ""}
    <div class="grid" id="grid"></div>
    <div class="infinite-status" id="person-grid-sentinel" aria-live="polite"></div>`;
  // Hiding from a person's own page leaves nowhere to stand, so it goes back
  // to the grid -- where the group it just removed is now absent.
  // From the page itself, a merge follows the survivor rather than dropping
  // you back on the grid: it is still the group you were looking at.
  if (!r.hidden) {
    cardMenu(document.getElementById("personactions"),
      clusterMenuItems({ id, name: r.name, photos: r.photos }, backToPeople,
        merged => (merged && merged.id ? showPerson(merged.id) : backToPeople())));
  }
  // Undoing a change alters who this person is, so the page is re-read rather
  // than patched -- the honest response to "that merge never happened".
  mountHistory(() => showPerson(id));
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
      // Opened from here, the viewer's arrows walk this person's photos and
      // stop at the ends of them -- not the whole archive.
      galleryFromGrid("grid", personGalleryLabel());
    },
  });
}
export function backToPeople() {
  const m = document.getElementById("main");
  if (!STASHED_PEOPLE) { renderFaces(m); return; }
  S.facePerson = null;
  const saved = STASHED_PEOPLE; STASHED_PEOPLE = null;
  m.replaceChildren();
  m.appendChild(saved.fragment);
  // Scroll after the nodes are laid out, or the position is set against a
  // height the browser has not worked out yet.
  requestAnimationFrame(() => { m.scrollTop = saved.scrollTop; });
  startFacePoll();
  // The page just left may have renamed, hidden or merged this group, so the
  // restored grid is reconciled against the server -- in place, which is what
  // keeps the scroll position that was the point of restoring it.
  refreshAfterHide();
}
export function editPersonName(id, current) {
  const box = document.getElementById("personname"); if (!box) return;
  inlineNameEdit(box, {
    value: current,
    label: "Person’s name",
    className: "detail-name-input",
    onSave: (name, input) => savePersonName(id, name, input),
    onCancel: () => showPerson(id),
  });
}
async function savePersonName(id, name, inp) {
  inp.disabled = true;
  let r;
  try { r = await jpost("/api/faces/person/rename", { person_id: id, name }); }
  catch (e) { r = { error: String(e) }; }
  if (!r || r.error) {
    toast((r && r.error) ? ("Couldn’t save: " + r.error) : "Couldn’t save the person’s name.", true);
  }
  showPerson(id);
}
