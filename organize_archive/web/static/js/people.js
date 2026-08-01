// The People screen: the face-detection status panel and its poll, the people
// grid and its incremental resync, the "same person?" suggestion review, and
// the single-person page. Merging is drag-to-merge's job, not this module's.

import {
  renderNav,
} from "./router.js";
import {
  personTile,
} from "./library.js";
import {
  startInfiniteList,
} from "./infinite.js";
import {
  closeModal,
} from "./item.js";
import {
  jget, jpost,
} from "./api.js";
import {
  esc, setText, toast,
} from "./dom.js";
import {
  attachMergeDrag, guardCardClick, mergesPanel,
} from "./merge.js";
import {
  stopPoll,
} from "./overview.js";
import {
  S,
} from "./state.js";
import {
  detectStatusRow, syncCardGrid,
} from "./main.js";

export async function renderFaces(m) {
  const gen = S.nav, root = S.arch.id;
  S.facePerson = null;
  const sum = await jget("/api/faces/summary?root=" + root);
  if (gen !== S.nav) return;
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
export function startFacePoll() { stopPoll(); S.poll = setInterval(faceTick, 1500); faceTick(); }
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
export async function answerSuggest(kind) {
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
export async function hidePerson(id) {
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
export async function showPerson(id) {
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
export function backToPeople() { renderFaces(document.getElementById("main")); }
export function editPersonName(id, current) {
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
  closeModal(); S.nav++; S.section = "people"; renderNav(); showPerson(id);
}
