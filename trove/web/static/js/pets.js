// The Pets screen: likely pets, unassigned animals, and the non-human face
// review queue -- three grids over one detection pass, kept current by one
// poll. The single-pet page lives here too.

import {
  detectStatusRow, editNeighbourName, showStatusPanel, syncCardGrid, thumbCollage,
} from "./cards.js";
import {
  ACTIVE_SECTION, backControl, onBackControl, showSection,
} from "./router.js";
import {
  petTile,
} from "./library.js";
import {
  galleryFromGrid,
} from "./gallery.js";
import {
  startInfiniteList,
} from "./infinite.js";
import {
  openItem,
} from "./item.js";
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
  askConfirm, attachMergeDrag, guardCardClick, mergeWithPicker,
} from "./merge.js";
import {
  cardMenu,
} from "./menu.js";
import {
  inlineNameEdit,
} from "./nameedit.js";
import {
  onSnapshot,
} from "./pipeline.js";
import {
  openOrSelect, selectButton, selectable, syncSelectButton,
} from "./select.js";
import {
  S,
} from "./state.js";

const PET_LIST_PAGE_SIZE = 120, LOOSE_PET_PAGE_SIZE = 120, NONHUMAN_PAGE_SIZE = 60;
// Shared by the first render and by syncPetGrids, so an emptied grid says
// the same thing however it got there.
const PET_EMPTY = '<div class="muted">No repeated pets grouped yet.</div>',
      LOOSE_PET_EMPTY = '<div class="muted">No unassigned sightings.</div>',
      NONHUMAN_EMPTY = '<div class="muted">No pending non-human decisions.</div>';
const petStamp = sum => [sum.pets, sum.detections, sum.nonhuman_faces].join("/");
// As on People: one spelling of "looked at, of what there is to look at",
// written into the tile and into the copy on its definition.
function scannedFigure(sum) {
  return `${sum.scanned.toLocaleString()} <small>/ ${sum.total_images.toLocaleString()}</small>`;
}

export async function renderPets(m) {
  const gen = S.nav, root = S.arch.id;
  STASHED_PETS = null;   // a full render replaces whatever was set aside
  const sum = await jget("/api/pets/summary?root=" + root);
  if (gen !== S.nav) return;
  S.petSum = sum;
  S.petJobRunning = false; S.petStamp = petStamp(sum);
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Pets</h2>
      <p>Locally detected animals, likely identities, and non-human face review.</p></div>${docsButton("pets")}</div>
    <div class="statrow">
      <div class="stat"><div><div class="k">Likely pets</div><div class="v" id="ps-pets">${sum.pets.toLocaleString()}</div></div>
        ${why("Likely pets", sum.pets.toLocaleString(), "Animals seen often enough, and alike enough, to be grouped as one pet.")}</div>
      <div class="stat"><div><div class="k">Animals</div><div class="v" id="ps-detections">${sum.detections.toLocaleString()}</div></div>
        ${why("Animals", sum.detections.toLocaleString(), "Every animal spotted, before grouping. Two dogs in one photo count twice.")}</div>
      <div class="stat"><div><div class="k">Non-human faces</div><div class="v" id="ps-nonhuman">${sum.nonhuman_faces.toLocaleString()}</div></div>
        ${why("Non-human faces", sum.nonhuman_faces.toLocaleString(), "Faces the human check rejected, usually an animal's. Yours to review here.")}</div>
      <div class="stat"><div><div class="k">Scanned</div><div class="v" id="ps-scanned">${scannedFigure(sum)}</div></div>
        ${why("Scanned", scannedFigure(sum), "Photos animal detection has looked at, of all the photos it will look at.")}</div>
    </div>
    <div class="panel" id="petjob" hidden></div>
    <div class="place-gallery-head"><h3>Likely pet identities</h3>
      <span class="muted">Conservative visual grouping</span>${selectButton("pet")}</div>
    <div class="people" id="petgrid"></div>
    <div class="infinite-status" id="pet-list-sentinel" aria-live="polite"></div>
    <div id="pethiddenwrap"></div>
    <div class="place-gallery-head"><h3>Unassigned animals</h3><span class="muted">Single or uncertain sightings</span></div>
    <div class="people" id="loosepetgrid"></div>
    <div class="infinite-status" id="loose-pet-sentinel" aria-live="polite"></div>
    <div class="place-gallery-head"><h3>Non-human face review</h3><span class="muted">Animal/toy overlaps filtered out of People</span></div>
    <div class="nonhuman-grid" id="nonhumangrid"></div>
    <div class="infinite-status" id="nonhuman-sentinel" aria-live="polite"></div>`;
  renderPetStatus();
  selectable("pet", "petgrid", refreshPetGrids);

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
      syncSelectButton("pet");
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

  renderHiddenPets();
  startPetPoll();
}
/* The groups taken off the screen, kept reachable at the foot of it. Twin of
   the People screen's Unknown section, down to being absent when empty. */
const HIDDEN_PET_PAGE_SIZE = 120;
async function renderHiddenPets() {
  const wrap = document.getElementById("pethiddenwrap"); if (!wrap) return;
  const n = (S.petSum && S.petSum.hidden_pets) || 0;
  const previous = document.querySelector(".hidden-people");
  const wasOpen = !!previous && previous.hasAttribute("open");
  if (!n) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<details class="hidden-people"${wasOpen ? " open" : ""}>
      <summary>Unknown <span class="muted">\u00b7 ${n.toLocaleString()} group${n === 1 ? "" : "s"}</span></summary>
      <div class="people" id="hiddenpetgrid"></div>
      <div class="infinite-status" id="hidden-pet-sentinel" aria-live="polite"></div>
    </details>`;
  startInfiniteList("hiddenPetList", {
    sentinelId: "hidden-pet-sentinel", pageSize: HIDDEN_PET_PAGE_SIZE,
    fetchPage: async offset => {
      const res = await jget(`/api/pets?root=${S.arch.id}&hidden=1&offset=${offset}&limit=${HIDDEN_PET_PAGE_SIZE}`);
      return res.pets;
    },
    onPage: (pets, { first }) => {
      const grid = document.getElementById("hiddenpetgrid"); if (!grid) return;
      if (first) grid.innerHTML = "";
      pets.forEach(p => grid.appendChild(hiddenPetCard(p)));
    },
  });
}
function hiddenPetCard(p) {
  const d = document.createElement("div"); d.className = "pcard is-hidden";
  d.dataset.syncKey = String(p.id);
  d.innerHTML = thumbCollage(petCoverIds(p), "/animalThumb") + `<div class="pmeta">
      <div class="pmeta-text">
        <div class="pname ${p.name ? "" : "un"}">${p.name ? esc(p.name) : "Unnamed group"}</div>
        <div class="pcount">${fileCount(p.photos)}</div>
      </div>
      <button class="quietbtn sm" type="button">Put back</button></div>`;
  d.querySelector("button").onclick = e => { e.stopPropagation(); unhidePet(p.id); };
  return d;
}
function petMetaInner(p) {
  return `<div class="pmeta-text">
      <button class="pname ${p.name ? "" : "un"}" type="button">${esc(p.name || "Name this pet")}</button>
      <div class="pcount">${fileCount(p.photos)}</div>
      <span class="pet-species">${esc(p.species)}</span></div>`;
}
/* What a pet's card and its own page can do to the whole group. The People
   screen's twin is clusterMenuItems; the wording differs because the claims
   do -- "Not an animal" is about the detections, "Unknown animal" is about
   whether you want the group listed. */
function petMenuItems(p, after, onMerged) {
  return [
    {
      label: "Merge with\u2026",
      submenu: (panel, close) => mergeWithPicker(
        panel, close, { kind: "pet", id: p.id, name: p.name, photos: p.photos || 0 }, onMerged),
    },
    {
      label: "Not an animal",
      danger: true,
      onPick: async () => {
        if (!await askConfirm({
          title: "Not an animal?",
          body: "Its photos are left out of pet grouping from now on. Nothing is deleted.",
          confirmLabel: "Not an animal", danger: true,
        })) return;
        await hidePetGroup(p.id, "not_animal", after);
      },
    },
    { label: "Unknown animal", onPick: () => hidePetGroup(p.id, "unknown", after) },
  ];
}
async function hidePetGroup(id, reason, after) {
  let res;
  try { res = await jpost("/api/pet/hide", { pet_id: id, reason }); }
  catch (e) { res = { error: String(e) }; }
  if (!res || res.error) { toast("Couldn\u2019t hide this group.", true); return; }
  if (reason === "unknown") toast("Moved to \u201cUnknown\u201d, at the foot of the screen. Put it back from there.");
  after();
}
export async function unhidePet(id) {
  let res;
  try { res = await jpost("/api/pet/unhide", { pet_id: id }); }
  catch (e) { res = { error: String(e) }; }
  if (!res || res.error) { toast("Couldn\u2019t restore this group.", true); return; }
  refreshPetGrids();
}
function petCoverIds(p) {
  return (p.detections_preview && p.detections_preview.length
    ? p.detections_preview : [p.cover_detection_id]).filter(Boolean).slice(0, 4);
}
function petCard(p) {
  const card = document.createElement("div"); card.className = "pcard";
  card.onclick = guardCardClick(() => openOrSelect("pet", p, () => showPet(p.id)));
  card.dataset.syncKey = String(p.id);
  card.dataset.cover = petCoverIds(p).join(",");
  card.innerHTML = thumbCollage(petCoverIds(p), "/animalThumb")
    + `<div class="pmeta">${petMetaInner(p)}</div>`;
  card.querySelector(".pname").onclick = e => { e.stopPropagation(); editPetCardName(card, p); };
  cardMenu(card.querySelector(".pmeta"), petMenuItems(p, refreshPetGrids, refreshPetGrids));
  // Mutable so syncPetGrids can refresh a renamed/re-counted pet without
  // re-running attachMergeDrag (which would stack a second set of listeners).
  card._merge = { kind: "pet", id: p.id, name: p.name, photos: p.photos };
  attachMergeDrag(card, card._merge, refreshPetGrids);
  return card;
}
// In-place refresh of one already-rendered pet card. The cover <img> is
// only reswapped when the cover detection actually changes, so a pet whose
// photo count ticked up mid-run doesn't visibly blink its thumbnail.
function updatePetCard(card, p) {
  const meta = card.querySelector(".pmeta");
  // Mid-rename: leave the card alone entirely, or the poll eats what is being
  // typed into it. Same guard, and the same reason, as updatePersonCard.
  if (meta && meta.classList.contains("pmeta-editing")) return false;
  // An unchanged collage keeps its <img> nodes, so a pet whose photo count
  // ticked up mid-run does not visibly blink its thumbnails.
  const cover = petCoverIds(p).join(",");
  if (card.dataset.cover !== cover) {
    card.dataset.cover = cover;
    card.firstElementChild.outerHTML = thumbCollage(petCoverIds(p), "/animalThumb");
  }
  if (meta) {
    meta.innerHTML = petMetaInner(p);
    meta.querySelector(".pname").onclick = e => { e.stopPropagation(); editPetCardName(card, p); };
    cardMenu(meta, petMenuItems(p, refreshPetGrids, refreshPetGrids));
  }
  Object.assign(card._merge, { name: p.name, photos: p.photos });
  return true;
}
function editPetCardName(card, p) {
  const meta = card.querySelector(".pmeta"); if (!meta) return;
  card.onclick = null;
  card.draggable = false;   // don't let an in-progress rename start a merge-drag
  meta.className = "pmeta pmeta-editing";
  inlineNameEdit(meta, {
    value: p.name,
    label: "Pet’s name",
    after: `<div class="pcount">${fileCount(p.photos)} · Enter or click away to save</div>`,
    onSave: (name, input, step) => savePetCardName(card, p, name, input, step),
    onCancel: () => card.replaceWith(petCard(p)),
    onStep: true,
  });
}
async function savePetCardName(card, p, name, input, step = 0) {
  if (name === (p.name || "")) {
    card.replaceWith(petCard(p));
    editNeighbourName("petgrid", p.id, step);
    return;
  }
  input.disabled = true;
  let result;
  try { result = await jpost("/api/pet/rename", { pet_id: p.id, name }); }
  catch (e) { result = { error: String(e) }; }
  if (!result || result.error) {
    toast("Couldn’t save the pet’s name.", true); card.replaceWith(petCard(p)); return;
  }
  card.replaceWith(petCard({ ...p, name: name || null }));
  editNeighbourName("petgrid", p.id, step);
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
  await patchPetGrids();
}
/* The same patch, after the user merged two pets rather than on the clock.

   This is what merging used to do instead: showSection("pets", true), which
   calls replaceChildren on the whole section and drops its cached view -- the
   entire screen torn down and rebuilt, losing the scroll position and every
   loaded page, once per merge. Sorting a shelf of animals into pets is dozens
   of merges in a row, so it was the worst place in the app to do the most
   destructive possible refresh. */
async function refreshPetGrids() {
  await patchPetGrids();
  // hidden_pets is what decides whether the Unknown section exists at all, and
  // the poll only refreshes the summary while a detect job runs -- so on a
  // settled archive nothing else would tell this screen what just happened.
  const sum = await jget("/api/pets/summary?root=" + S.arch.id).catch(() => null);
  if (sum) { S.petSum = sum; setStat("ps-pets", sum.pets.toLocaleString()); }
  await renderHiddenPets();
}
async function patchPetGrids() {
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
  } catch { return; }
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
  syncSelectButton("pet");
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
export function startPetPoll() { stopPetPoll(); petPoll = setInterval(petTick, 1800); petTick(); }
export function stopPetPoll() { if (petPoll) { clearInterval(petPoll); petPoll = null; } }
let petPoll = null;
// The detect stage comes from the one poller rather than being fetched here.
// Same reasoning as People's: two requests per tick for a snapshot the sidebar
// chip already held is what emptied the connection budget while one was slow.
onSnapshot(() => renderPetStatus());
// The summary and the detect stage now arrive separately, so the row is drawn
// from whatever state holds rather than from arguments one caller happens to
// have. Same shape as People's renderFaceStatus.
function renderPetStatus() {
  if (!S.petSum) return;
  showStatusPanel("petjob", detectStatusRow(S.petSum, null));
}
const detectRunning = () =>
  ((S.pipeline && S.pipeline.stages) || []).some(s => s.id === "detect" && s.state === "running");
// Mirrors faceTick: the stat tiles and status row tick every poll, and the
// grids are *patched* (syncPetGrids) rather than rebuilt, so the page never
// resets under the user -- scroll position, the pages the infinite lists
// have already loaded and any half-finished non-human review all survive,
// and only cards whose data actually changed are touched.
const petTick = oneAtATime(async () => {
  if (ACTIVE_SECTION !== "pets") { stopPetPoll(); return; }
  const area = document.getElementById("petjob"); if (!area) { stopPetPoll(); return; }
  const sum = await jget("/api/pets/summary?root=" + S.arch.id);
  S.petSum = sum;
  const running = detectRunning();
  const was = S.petJobRunning; S.petJobRunning = running;
  setStat("ps-pets", sum.pets.toLocaleString());
  setStat("ps-detections", sum.detections.toLocaleString());
  setStat("ps-nonhuman", sum.nonhuman_faces.toLocaleString());
  setStat("ps-scanned", scannedFigure(sum));
  renderPetStatus();
  // Three list endpoints are worth refetching only when something actually
  // moved; the run's finishing edge always gets one last pass.
  const stamp = petStamp(sum);
  if (running ? stamp !== S.petStamp : was) syncPetGrids();
  S.petStamp = stamp;
});
async function reviewNonhuman(id, verdict, card) {
  const result = await jpost("/api/nonhuman/review", { detection_id: id, verdict });
  if (result.error) { toast(result.error, true); return; }
  card.remove();
  toast(verdict === "human" ? "Restored to People for the next clustering pass." : "Confirmed as non-human.");
}
const PET_DETAIL_PAGE_SIZE = 120;
/* The cover, which is what "Make cover photo" sets; the first photo with a
   detection only until one has been derived. Taking that fallback first is
   what kept a chosen cover off the People page for a while. */
const petAvatar = pet =>
  pet.cover_detection_id || (pet.items.find(it => it.detection_id) || {}).detection_id || 0;
/* The three grids, set aside while a pet's page is open. See the People
   screen's twin for why: opening a pet is not a section change, so nothing
   else keeps the scroll position or the pages already loaded. */
let STASHED_PETS = null;
export function backToPets() {
  const m = document.getElementById("main");
  if (!STASHED_PETS) { showSection("pets", true); return; }
  const saved = STASHED_PETS; STASHED_PETS = null;
  m.replaceChildren();
  m.appendChild(saved.fragment);
  requestAnimationFrame(() => { m.scrollTop = saved.scrollTop; });
  startPetPoll();
  // The page just left may have renamed, hidden or merged this group.
  refreshPetGrids();
}
export async function showPet(id) {
  stopPetPoll(); const m = document.getElementById("main");
  if (!STASHED_PETS) {
    const fragment = document.createDocumentFragment();
    const scrollTop = m.scrollTop;
    while (m.firstChild) fragment.appendChild(m.firstChild);
    STASHED_PETS = { fragment, scrollTop };
  }
  m.innerHTML = '<div class="muted" style="padding:30px">Loading…</div>';
  const pet = await jget(`/api/pet/${id}?root=${S.arch.id}&limit=${PET_DETAIL_PAGE_SIZE}`);
  if (!pet || pet.error) { m.innerHTML = '<div class="soonbox">Pet not found.</div>'; return; }
  S.currentPet = pet;
  const name = pet.name || "Name this pet";
  m.innerHTML = `<div class="facetopbar">${backControl("Pets")}
    <img class="person-header-avatar" src="/animalThumb/${petAvatar(pet)}" alt="">
    <div class="ftb-identity"><div class="ftb-name" id="petname"><button class="person-name-button" type="button" data-tip="Rename this pet"><span>${esc(name)}</span>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.2-1 10.7-10.7a2.1 2.1 0 0 0-3-3L5.2 16 4 20Z"/><path d="m14.5 6.5 3 3"/></svg></button></div>
    <span class="muted ftb-count">${esc(pet.species)} · ${fileCount(pet.photos)}</span></div>
    ${historyButton("pet", pet.id, pet.name)}
    <div class="ftb-actions" id="petactions"></div></div>
    <div class="grid" id="grid"></div>
    <div class="infinite-status" id="pet-grid-sentinel" aria-live="polite"></div>`;
  onBackControl(m, backToPets);
  document.querySelector("#petname .person-name-button")
    .addEventListener("click", () => editPetName(id, pet.name || ""));
  cardMenu(document.getElementById("petactions"), petMenuItems(
    pet,
    backToPets,
    merged => (merged && merged.id ? showPet(merged.id) : backToPets()),
  ));
  mountHistory(() => showPet(id));
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
      items.forEach(item => grid.appendChild(petTile(item, id)));
      galleryFromGrid("grid", (S.currentPet && S.currentPet.name)
        ? `in ${S.currentPet.name}\u2019s photos` : "in this pet\u2019s photos");
    },
  });
}
function editPetName(id, current) {
  const box = document.getElementById("petname"); if (!box) return;
  inlineNameEdit(box, {
    value: current,
    label: "Pet’s name",
    className: "detail-name-input",
    onSave: (name, input) => savePetName(id, name, input),
    onCancel: () => showPet(id),
  });
}
async function savePetName(id, name, inp) {
  inp.disabled = true;
  let r;
  try { r = await jpost("/api/pet/rename", { pet_id: id, name }); }
  catch (e) { r = { error: String(e) }; }
  if (!r || r.error) {
    toast((r && r.error) ? ("Couldn’t save: " + r.error) : "Couldn’t save the pet’s name.", true);
  }
  showPet(id);
}
