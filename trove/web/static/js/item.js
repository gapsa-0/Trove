// The detail modal: what one item shows, and everything editable on it --
// reassigning a face, adding a manual person or pet tag, correcting the date at
// whatever precision is known, and attaching or creating a place. The place
// picker's own small map is private here; syncPickerMapTiles() is the theme
// switch's only way in.

import {
  jget, qpost,
} from "./api.js";
import {
  esc, fmtBytes, fmtDate, toast,
} from "./dom.js";
import {
  MAP_WORLD_BOUNDS, configureMapViewport, replaceMapTiles, themedTileLayer,
} from "./places.js";
import {
  TYPE_ICON, typeLabel,
} from "./state.js";

export let MITEM = null;                 // the currently-open item, mutated in place on edit
export async function openItem(id) {
  MITEM = await jget("/api/item/" + id);
  const m = document.getElementById("mmedia");
  if (MITEM.type === "image") m.innerHTML = `<img src="/file/${id}">`;
  else if (MITEM.type === "video") m.innerHTML = `<video src="/file/${id}" controls autoplay></video>`;
  else if (MITEM.type === "audio") m.innerHTML = `<div style="padding:40px"><div class="ph" style="font-size:60px;text-align:center">🎵</div><audio src="/file/${id}" controls autoplay></audio></div>`;
  else m.innerHTML = `<div class="ph" style="font-size:70px;padding:60px">${TYPE_ICON[MITEM.type] || "📦"}</div>`;
  renderInfo();
  document.getElementById("modal").classList.add("open");
}
export function renderInfo() {
  closePick();
  const it = MITEM;
  const kv = (k, v) => v != null && v !== "" ? `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>` : "";
  const dims = it.meta && it.meta.width ? `${it.meta.width}×${it.meta.height}` : "";
  const cam = it.meta && it.meta.model ? ((it.meta.make || "") + " " + it.meta.model).trim() : "";
  const gps = it.gps ? `<a href="https://www.openstreetmap.org/?mlat=${it.gps.lat}&mlon=${it.gps.lon}&zoom=14" target="_blank">${it.gps.lat.toFixed(5)}, ${it.gps.lon.toFixed(5)}</a>` : "";
  const dsrc = it.date && it.date_source ? `<span class="muted" style="font-size:11px"> · ${it.date_source}</span>` : "";
  // faces (detected) + manual person tags, unioned in one list
  const faceRows = (it.people || []).map(faceRow).join("") +
    (it.manual_people || []).map(manualPersonRow).join("");
  let faces;
  if (faceRows) faces = `<div class="facelist">${faceRows}</div>`;
  else if (it.type !== "image" && it.type !== "video")
    faces = `<div class="muted" style="font-size:12px">Face detection runs on photos and videos.</div>`;
  else faces = `<div class="muted" style="font-size:12px">No faces detected.</div>`;
  // pets (detected) + manual pet tags
  const animalRows = (it.animals || []).map(a => `<div class="facerow">
        <img class="facecrop" src="/animalThumb/${a.detection_id}" loading="lazy">
        <span>${a.name ? `<strong>${esc(a.name)}</strong> ` : ""}<span class="pet-species">${esc(a.species)}</span>
        <span class="muted">${Math.round(a.score * 100)}%</span></span></div>`).join("") +
    (it.manual_pets || []).map(manualPetRow).join("");
  const animals = animalRows ? `<div class="facelist">${animalRows}</div>`
    : `<div class="muted" style="font-size:12px">No pets detected.</div>`;
  // place
  const placeTxt = it.place ? (it.place.name ? esc(it.place.name) : '<span class="muted">Name this place</span>')
    : '<span class="muted">No place set</span>';
  document.getElementById("minfo").innerHTML = `<h3>${esc(it.name)}</h3>` +
    `<div class="isec"><div class="h">People <button class="linkbtn" onclick="addPersonPicker()">Add</button></div>
       <div id="people-add"></div>${faces}</div>` +
    `<div class="isec"><div class="h">Pets <button class="linkbtn" onclick="addPetPicker()">Add</button></div>
       <div id="pet-add"></div>${animals}</div>` +
    `<div class="isec"><div class="h">Place <button class="linkbtn" onclick="editPlace()">Change</button></div>
       <div id="placeval" class="val">${placeTxt}</div></div>` +
    `<div class="isec"><div class="h">Date <button class="linkbtn" onclick="editDate()">Edit</button></div>
       <div id="dateval" class="val">${fmtDate(it.date)}${dsrc}</div></div>` +
    `<div class="isec"><div class="h">Details</div>` +
    kv("Type", typeLabel(it.type)) + kv("Size", fmtBytes(it.size)) + kv("Dimensions", dims) + kv("Camera", cam) +
    kv("Coordinates", gps) + kv("Description", it.description ? esc(it.description) : "") + `</div>` +
    `<div class="isec"><div class="h">File</div>
       <div style="font-size:11px;color:var(--muted);word-break:break-all">${esc(it.rel_path)}</div>
       <div style="margin-top:10px"><a href="/file/${it.id}" target="_blank">Open original ↗</a></div></div>`;
}
// Optimistic saves: update the panel now, persist in the background, and roll back
// only if the DB write actually fails, so editing feels instant even while the
// pipeline holds the single writer. Every background callback bails out (or re-checks
// stillOpen) if the modal has since closed or moved to another item.
function faceRow(f) {
  const named = MITEM.person_options || [];
  const isNamed = f.person_id && f.name;
  let opts = isNamed ? "" : `<option value="" selected>${f.name ? esc(f.name) : "unknown"}</option>`;
  named.forEach(p => { opts += `<option value="${p.id}"${p.id === f.person_id ? " selected" : ""}>${esc(p.name)}</option>`; });
  if (!named.length && !isNamed)
    return `<div class="facerow"><img class="facecrop" src="/faceThumb/${f.face_id}" loading="lazy" onerror="this.style.visibility='hidden'">
      <span class="muted" style="font-size:12px">Name people in the People section to label them here.</span></div>`;
  return `<div class="facerow">
    <img class="facecrop" src="/faceThumb/${f.face_id}" loading="lazy" onerror="this.style.visibility='hidden'">
    <select class="fsel" title="Reassign this face" onchange="reassignFace(${f.face_id},this.value,this)">${opts}</select></div>`;
}
function stillOpen(id) { return MITEM && MITEM.id === id; }
export function reassignFace(faceId, pid, sel) {
  if (!pid) return;
  const f = (MITEM.people || []).find(x => x.face_id === faceId);
  const prev = f ? { person_id: f.person_id, name: f.name } : null;
  const opt = (MITEM.person_options || []).find(p => p.id === +pid);
  if (f && opt) { f.person_id = opt.id; f.name = opt.name; }   // optimistic (the select already shows it)
  flashSaved(sel);
  const revert = (msg) => {
    if (f && prev) { f.person_id = prev.person_id; f.name = prev.name; }
    if (sel.isConnected) sel.value = (prev && prev.name) ? String(prev.person_id) : "";
    toast(msg, true);
  };
  qpost("/api/faces/reassign", { face_id: faceId, person_id: +pid })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t reassign that face: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t reassign that face: connection error"));
}
function flashSaved(el) {
  const o = el.style.borderColor; el.style.transition = "border-color .2s";
  el.style.borderColor = "var(--good)"; setTimeout(() => { el.style.borderColor = o; el.style.transition = ""; }, 900);
}
/* ----- manual people/pet tags: for media with no face/pet detected at all
   (back of a head, missed angle, group shot the detector skipped). Only
   named people/pets are offered (person_options/pet_options already
   filter to named ones), same discipline as reassignFace: mutate MITEM
   and repaint now, POST in the background, roll back + toast only on an
   actual failure, bail via stillOpen if the user has moved on. ----- */
function manualPersonRow(p) {
  return `<div class="facerow">
    <div class="facecrop placeholder">👤</div>
    <span style="flex:1;min-width:0"><strong>${esc(p.name)}</strong></span>
    <button class="linkbtn" onclick="removeManualPerson(${p.person_id})">Remove</button></div>`;
}
function manualPetRow(p) {
  return `<div class="facerow">
    <div class="facecrop placeholder">🐾</div>
    <span style="flex:1;min-width:0"><strong>${esc(p.name)}</strong></span>
    <button class="linkbtn" onclick="removeManualPet(${p.pet_id})">Remove</button></div>`;
}
function _taggedPersonIds(it) {
  const ids = new Set((it.people || []).filter(f => f.person_id).map(f => f.person_id));
  (it.manual_people || []).forEach(p => ids.add(p.person_id));
  return ids;
}
function _taggedPetIds(it) {
  const ids = new Set((it.animals || []).filter(a => a.pet_id).map(a => a.pet_id));
  (it.manual_pets || []).forEach(p => ids.add(p.pet_id));
  return ids;
}
export function addPersonPicker() {
  const it = MITEM, host = document.getElementById("people-add");
  if (!host) return;
  if (!(it.person_options || []).length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Name people in the People section to label them here.</div>`;
    return;
  }
  const present = _taggedPersonIds(it);
  const avail = it.person_options.filter(p => !present.has(p.id));
  if (!avail.length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Everyone named is already tagged here.</div>`;
    return;
  }
  let sel = `<select class="fsel" onchange="onAddPerson(this.value)"><option value="" selected>Add a person…</option>`;
  avail.forEach(p => sel += `<option value="${p.id}">${esc(p.name)}</option>`);
  host.innerHTML = sel + `</select>`;
}
export function onAddPerson(pid) {
  if (!pid) return;
  const it = MITEM, id = it.id;
  const opt = (it.person_options || []).find(p => p.id === +pid);
  if (!opt) return;
  it.manual_people = it.manual_people || [];
  it.manual_people.push({ person_id: opt.id, name: opt.name });
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) {
      it.manual_people = it.manual_people.filter(p => p.person_id !== opt.id);
      renderInfo();
    }
    toast(msg, true);
  };
  qpost("/api/item/person/add", { person_id: +pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t add that person: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t add that person: connection error"));
}
export function removeManualPerson(pid) {
  const it = MITEM, id = it.id;
  const idx = (it.manual_people || []).findIndex(p => p.person_id === pid);
  if (idx < 0) return;
  const removed = it.manual_people[idx];
  it.manual_people.splice(idx, 1);
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) { it.manual_people.splice(idx, 0, removed); renderInfo(); }
    toast(msg, true);
  };
  qpost("/api/item/person/remove", { person_id: pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t remove that tag: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t remove that tag: connection error"));
}
export function addPetPicker() {
  const it = MITEM, host = document.getElementById("pet-add");
  if (!host) return;
  if (!(it.pet_options || []).length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Name pets in the Pets section to label them here.</div>`;
    return;
  }
  const present = _taggedPetIds(it);
  const avail = it.pet_options.filter(p => !present.has(p.id));
  if (!avail.length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;margin:2px 0 8px">Every named pet is already tagged here.</div>`;
    return;
  }
  let sel = `<select class="fsel" onchange="onAddPet(this.value)"><option value="" selected>Add a pet…</option>`;
  avail.forEach(p => sel += `<option value="${p.id}">${esc(p.name)}</option>`);
  host.innerHTML = sel + `</select>`;
}
export function onAddPet(pid) {
  if (!pid) return;
  const it = MITEM, id = it.id;
  const opt = (it.pet_options || []).find(p => p.id === +pid);
  if (!opt) return;
  it.manual_pets = it.manual_pets || [];
  it.manual_pets.push({ pet_id: opt.id, name: opt.name });
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) {
      it.manual_pets = it.manual_pets.filter(p => p.pet_id !== opt.id);
      renderInfo();
    }
    toast(msg, true);
  };
  qpost("/api/item/pet/add", { pet_id: +pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t add that pet: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t add that pet: connection error"));
}
export function removeManualPet(pid) {
  const it = MITEM, id = it.id;
  const idx = (it.manual_pets || []).findIndex(p => p.pet_id === pid);
  if (idx < 0) return;
  const removed = it.manual_pets[idx];
  it.manual_pets.splice(idx, 1);
  renderInfo();
  const revert = (msg) => {
    if (stillOpen(id)) { it.manual_pets.splice(idx, 0, removed); renderInfo(); }
    toast(msg, true);
  };
  qpost("/api/item/pet/remove", { pet_id: pid, file_id: id })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t remove that tag: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t remove that tag: connection error"));
}
export function editDate() {
  const p = (MITEM.date || "").split("T")[0].split("-");
  document.getElementById("dateval").innerHTML = `
    <div class="dtrow">
      <input class="yr" id="d-y" type="text" inputmode="numeric" maxlength="4" placeholder="Year" value="${p[0] || ""}">
      <input id="d-m" type="text" inputmode="numeric" maxlength="2" placeholder="Mon" value="${p[1] ? (+p[1]) : ""}">
      <input id="d-d" type="text" inputmode="numeric" maxlength="2" placeholder="Day" value="${p[2] ? (+p[2]) : ""}">
    </div>
    <div class="muted" style="font-size:11px">Enter only what you know; year alone is fine.</div>
    <div class="btnrow"><button class="btn" onclick="saveDate()">Save</button>
      <button class="btn sec" onclick="renderInfo()">Cancel</button></div>`;
  document.getElementById("d-y").focus();
}
export function saveDate() {
  const y = document.getElementById("d-y").value.trim(),
    mo = document.getElementById("d-m").value.trim(),
    da = document.getElementById("d-d").value.trim();
  if (!y) { toast("Year is required.", true); return; }
  const pad = v => String(v).padStart(2, "0");
  let v = String(+y);
  if (mo) { v += "-" + pad(+mo); if (da) v += "-" + pad(+da); }   // day needs a month
  const id = MITEM.id, prev = { date: MITEM.date, src: MITEM.date_source };
  MITEM.date = v; MITEM.date_source = "manual"; renderInfo();   // instant
  const revert = (msg) => {
    if (stillOpen(id)) { MITEM.date = prev.date; MITEM.date_source = prev.src; renderInfo(); }
    toast(msg, true);
  };
  qpost("/api/item/date", { file_id: id, datetime: v })
    .then(r => { if (!(r && r.ok)) revert("Couldn’t save the date: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t save the date: connection error"));
}
export function editPlace() {
  const cur = MITEM.place ? MITEM.place.id : "";
  let sel = `<select class="fsel" onchange="onPlaceSelect(this.value)"><option value="">No place</option>`;
  (MITEM.place_options || []).forEach(p => sel += `<option value="${p.id}"${p.id === cur ? " selected" : ""}>${esc(p.name)}</option>`);
  sel += `</select>`;
  document.getElementById("placeval").innerHTML = sel +
    `<div class="btnrow"><button class="btn sec" onclick="newPlace()">＋ New place</button>
       <button class="linkbtn" onclick="renderInfo()">Done</button></div>
     <div id="p-pick"></div>`;
}
export function onPlaceSelect(pid) {
  const id = MITEM.id, prev = MITEM.place;
  MITEM.place = pid ? ((MITEM.place_options || []).find(p => p.id === +pid) || { id: +pid, name: null }) : null;
  renderInfo();   // instant; collapses the editor back to display
  const body = pid ? { file_id: id, place_id: +pid } : { file_id: id, clear: true };
  const revert = (msg) => { if (stillOpen(id)) { MITEM.place = prev; renderInfo(); } toast(msg, true); };
  qpost("/api/item/place", body)
    .then(r => { if (!(r && r.ok)) revert("Couldn’t update the place: " + ((r && r.error) || "try again")); })
    .catch(() => revert("Couldn’t update the place: connection error"));
}
let MPICK = null, MPICK_TILES = null, MPICK_MARK = null, MPICK_LL = null;
export function closePick() { if (MPICK) { MPICK.remove(); MPICK = null; } MPICK_TILES = null; MPICK_MARK = null; MPICK_LL = null; }
// The theme switch's one reach into the place picker's map, matching the
// Places map's own seam: the switch swaps tiles, it does not own the handle.
export function syncPickerMapTiles() {
  if (MPICK) MPICK_TILES = replaceMapTiles(MPICK, MPICK_TILES);
}
export function newPlace() {
  const host = document.getElementById("p-pick");
  host.innerHTML = `
    <input id="np-name" placeholder="Place name (e.g. Casa abuela)"
      style="width:100%;padding:7px 8px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);font:inherit;margin:8px 0 0;box-sizing:border-box">
    <div class="placepick" id="np-map"></div>
    <div class="muted" style="font-size:11px;margin:-2px 0 2px">Click the map to drop a pin; that becomes the place’s location.</div>
    <div class="btnrow"><button class="btn" id="np-save" onclick="saveNewPlace()" disabled>Create & attach</button>
      <button class="btn sec" onclick="closePick();editPlace()">Cancel</button></div>`;
  const start = MITEM.gps ? [MITEM.gps.lat, MITEM.gps.lon] : null;
  MPICK = L.map("np-map", {
    worldCopyJump: true, zoomSnap: 0,
    maxBounds: MAP_WORLD_BOUNDS, maxBoundsViscosity: 1
  });
  configureMapViewport(MPICK);
  MPICK_TILES = themedTileLayer().addTo(MPICK);
  if (start) { MPICK.setView(start, 15); dropPin(start[0], start[1]); } else MPICK.setView([20, 0], 1);
  MPICK.on("click", e => dropPin(e.latlng.lat, e.latlng.lng));
  setTimeout(() => MPICK && MPICK.invalidateSize(), 60);
}
function dropPin(lat, lon) {
  MPICK_LL = { lat, lon };
  if (MPICK_MARK) MPICK_MARK.setLatLng([lat, lon]);
  else MPICK_MARK = L.circleMarker([lat, lon], { radius: 8, weight: 2, color: "#fff", fillColor: "#3a7bd5", fillOpacity: 1 }).addTo(MPICK);
  const b = document.getElementById("np-save"); if (b) b.disabled = false;
}
export function saveNewPlace() {
  if (!MPICK_LL) { toast("Click the map to set the location first.", true); return; }
  const id = MITEM.id, root = MITEM.root_id, prev = MITEM.place;
  const name = (document.getElementById("np-name").value || "").trim();
  const ll = { lat: MPICK_LL.lat, lon: MPICK_LL.lon };
  closePick();
  MITEM.place = { id: null, name: name || null }; renderInfo();   // instant (id filled in when it lands)
  qpost("/api/places/create", { root, name, lat: ll.lat, lon: ll.lon, file_id: id }).then(r => {
    if (r && r.ok) {
      if (stillOpen(id)) {
        MITEM.place = r.place;
        if (r.place && r.place.name) (MITEM.place_options = MITEM.place_options || []).push(r.place);
        renderInfo();
      }
    } else {
      if (stillOpen(id)) { MITEM.place = prev; renderInfo(); }
      toast("Couldn’t create the place: " + ((r && r.error) || "try again"), true);
    }
  }).catch(() => {
    if (stillOpen(id)) { MITEM.place = prev; renderInfo(); }
    toast("Couldn’t create the place: connection error", true);
  });
}
export function closeModal() {
  closePick(); document.getElementById("modal").classList.remove("open");
  document.getElementById("mmedia").innerHTML = ""; MITEM = null;
}
