// The Pets screen: likely pets, unassigned animals, and the non-human face
// review queue -- three grids over one detection pass, kept current by one
// poll. The single-pet page lives here too.

import {
  ACTIVE_SECTION, showSection,
} from "./router.js";
import {
  tile,
} from "./library.js";
import {
  startInfiniteList,
} from "./infinite.js";
import {
  openItem,
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

const PET_LIST_PAGE_SIZE = 120, LOOSE_PET_PAGE_SIZE = 120, NONHUMAN_PAGE_SIZE = 60;
// Shared by the first render and by syncPetGrids, so an emptied grid says
// the same thing however it got there.
const PET_EMPTY = '<div class="muted">No repeated pets grouped yet.</div>',
      LOOSE_PET_EMPTY = '<div class="muted">No unassigned sightings.</div>',
      NONHUMAN_EMPTY = '<div class="muted">No pending non-human decisions.</div>';
const petStamp = sum => [sum.pets, sum.detections, sum.nonhuman_faces].join("/");
export async function renderPets(m) {
  const gen = S.nav, root = S.arch.id;
  const sum = await jget("/api/pets/summary?root=" + root);
  if (gen !== S.nav) return;
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
export function startPetPoll() { stopPoll(); S.poll = setInterval(petTick, 1800); petTick(); }
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
export async function showPet(id) {
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
export async function renamePet(id) {
  const name = prompt("Pet name", (S.currentPet && S.currentPet.name) || ""); if (name === null) return;
  const result = await jpost("/api/pet/rename", { pet_id: id, name: name.trim() });
  if (result.error) { toast(result.error, true); return; } showPet(id);
}
