// The Places screen: the Leaflet map, the Places/Photos view switch, the place
// gallery and the per-cluster side panel. All of the map's Leaflet handles are
// private to this module; disposeMap() and syncPlacesMapTiles() are the only
// two ways the router and the theme switch reach them.

import {
  attachMergeDrag, guardCardClick, mergesPanel,
} from "./merge.js";
import {
  jget, jpost,
} from "./api.js";
import {
  esc, toast,
} from "./dom.js";
import {
  S,
} from "./state.js";
import {
  NAV, currentTheme, openItem, startInfiniteList, tile,
} from "./main.js";

/* ---------- map (Leaflet: place clusters over OpenStreetMap) ----------
   Photos within 300m of each other are grouped server-side into one named
   "place" (organize_archive/geo/clusters.py). The screen-pixel bucketing
   below is a second, purely visual layer on top of that: at far zoom it
   still merges nearby PLACES into one numbered bubble; at close zoom each
   place stands alone as a small thumbnail collage + name. ---------- */
const MAP_TILE_STYLES = {
  light: {
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    options: { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }
  },
  dark: {
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    options: { maxZoom: 20, subdomains: "abcd", attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>' }
  }
};
export const MAP_WORLD_BOUNDS = L.latLngBounds(
  [[-85.0511287798, -1000000], [85.0511287798, 1000000]]);
export function themedTileLayer() {
  const style = MAP_TILE_STYLES[currentTheme()];
  return L.tileLayer(style.url, style.options);
}
function syncMapZoomFloor(map) {
  if (!map) return;
  // A Web Mercator world is 256 px tall at zoom 0. This fractional zoom
  // makes it exactly as tall as the map, so zooming out can never expose
  // empty space beyond its north or south edge.
  const minZoom = Math.log(map.getSize().y / 256) / Math.LN2;
  map.setMinZoom(minZoom);
}
export function configureMapViewport(map) {
  syncMapZoomFloor(map);
  map.on("resize", () => syncMapZoomFloor(map));
}
export function replaceMapTiles(map, tiles) {
  if (!map) return tiles;
  if (tiles) map.removeLayer(tiles);
  return themedTileLayer().addTo(map);
}
// The two seams the Places map offers the rest of the app. Both exist because
// the router and the theme switch have to reach into the map's private state,
// and neither of them should own a Leaflet handle to do it.
export function syncPlacesMapTiles() {
  if (MAP) MAP_TILES = replaceMapTiles(MAP, MAP_TILES);
}
export function disposeMap() {
  if (MAP) { MAP.remove(); MAP = null; MAP_LAYER = null; MAP_TILES = null; }
}
export let MAP = null, MAP_LAYER = null, MAP_TILES = null, MAP_CLUSTERS = [], MAP_HIDDEN = {};
// Un-clustered view (things_to_fix #33): every geotagged file as its own
// point. Fetched once, lazily, the first time the user asks for it -- it is
// a much bigger payload than the ~hundreds of place centroids, and most
// visits never leave the clustered view.
let MAP_POINTS = null, MAP_POINTS_UNPLACED = 0, MAP_POINT_CANVAS = null;
// The built point layer and what it was built from (see showPhotoPoints).
let MAP_POINT_LAYER = null, MAP_POINT_BUILT = null;
export async function renderMap(m) {
  const gen = NAV, root = S.arch.id;
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Places</h2>
      <p>Explore geolocated media and give meaningful names to the places you return to.</p></div></div>
    <div class="statrow map-stats">
      <div class="stat"><div><div class="k">Photos in places</div><div class="v" id="map-photo-count">-</div></div></div>
      <div class="stat"><div><div class="k">Places</div><div class="v" id="map-place-count">-</div></div></div>
      <div class="stat"><div><div class="k">Named places</div><div class="v" id="map-named-count">-</div></div></div>
    </div>
    <div class="mapwrap">
      <div id="lmap"></div>
      <div id="mapside"></div>
    </div>
    <div class="map-footnote" id="map-view-note"></div>
    <div class="map-footnote" id="map-hidden-note" hidden></div>
    <div id="placegallery"></div>`;
  if (MAP) { MAP.remove(); MAP = null; MAP_LAYER = null; }
  MAP_POINT_CANVAS = null; MAP_POINT_LAYER = null; MAP_POINT_BUILT = null;
  S.mapSel = null;
  // The view choice is a preference and survives; the points themselves are
  // this archive's data and are re-fetched on demand (see setMapView).
  S.mapView = S.mapView || "places";
  MAP_POINTS = null; MAP_POINTS_UNPLACED = 0;
  const { clusters, hidden } = await jget("/api/map/clusters?root=" + root);
  if (gen !== NAV) return;
  MAP_CLUSTERS = clusters;
  MAP_HIDDEN = hidden || {};
  updateMapStats();
  renderPlaceGallery();
  MAP = L.map("lmap", {
    worldCopyJump: true, zoomSnap: 0,
    maxBounds: MAP_WORLD_BOUNDS, maxBoundsViscosity: 1
  });
  configureMapViewport(MAP);
  MAP_TILES = themedTileLayer().addTo(MAP);
  if (!clusters.length) {
    MAP.setView([0, 0], 2);
    renderMapViewNote();
    document.querySelector(".mapwrap").insertAdjacentHTML("beforeend", `<div class="map-empty">
      <div class="big">⌖</div><h3>No locations yet</h3>
      <p>Places will appear here automatically when Archive finds GPS information in EXIF or Takeout metadata.</p></div>`);
    return;
  }
  addMapViewToggle(MAP);
  const b = L.latLngBounds(clusters.map(c => [c.lat, c.lon]));
  MAP.fitBounds(b, { padding: [40, 40], maxZoom: 14 });
  MAP.on("moveend zoomend", drawMap);
  drawMap();
  renderMapViewNote();
}
/* -- Places / Photos switch -------------------------------------------
   Two honest answers to different questions: the clustered view says where
   this family keeps going back to (one marker per place, however far its
   members are spread); the un-clustered one says where each photo was
   actually taken, which a centroid necessarily hides. */
function addMapViewToggle(map) {
  const Toggle = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const box = L.DomUtil.create("div", "map-viewtoggle");
      box.id = "map-viewtoggle";
      // Without this a click on the switch also reaches the map underneath
      // (and a double click zooms it).
      L.DomEvent.disableClickPropagation(box);
      return box;
    }
  });
  map.addControl(new Toggle());
  renderMapViewToggle();
}
function renderMapViewToggle(loading) {
  const box = document.getElementById("map-viewtoggle"); if (!box) return;
  const btn = (view, label) =>
    `<button type="button" class="${S.mapView === view ? "on" : ""}"
        ${loading ? "disabled" : ""} onclick="setMapView('${view}')">${label}</button>`;
  box.innerHTML = btn("places", "Places") + btn("photos", "Photos");
}
export async function setMapView(view) {
  if (!MAP || S.mapView === view) return;
  S.mapView = view;
  renderMapViewToggle(view === "photos" && !MAP_POINTS);
  if (view === "photos" && !MAP_POINTS) {
    try {
      await loadMapPoints();
    } catch {
      toast("Couldn’t load the individual photo locations.", true);
      S.mapView = "places";
    }
    if (S.section !== "places" || !MAP) return;   // user navigated away meanwhile
  }
  renderMapViewToggle();
  drawMap();
  renderMapViewNote();
}
async function loadMapPoints() {
  const r = await jget("/api/map/points?root=" + S.arch.id);
  MAP_POINTS = r.points || [];
  MAP_POINTS_UNPLACED = r.unplaced || 0;
}
// Anything that changes which places exist or which files they hold also
// changes a point's colour (naming a one-off spot promotes it out of grey,
// merging moves files between hues), so the cache is dropped and only
// re-pulled when that view is actually on screen.
async function invalidateMapPoints() {
  MAP_POINTS = null;
  if (S.mapView === "photos") {
    try {
      await loadMapPoints();
    } catch {
      // The cache is already cleared, so a failed re-pull costs nothing: the
      // next switch to the photos view fetches again.
    }
  }
}
function renderMapViewNote() {
  const el = document.getElementById("map-view-note"); if (!el) return;
  const tiles = "Street map tiles are fetched online using coordinates only; your media stays on this computer.";
  if (S.mapView !== "photos") {
    el.innerHTML = `Nearby photos are grouped into places. ${tiles}`;
    return;
  }
  // One swatch per place would be a legend of hundreds; say what the colours
  // mean instead, and name the grey exception explicitly.
  const strays = MAP_POINTS_UNPLACED
    ? ` <span class="map-pointkey"><i></i>Grey: ${MAP_POINTS_UNPLACED.toLocaleString()} photo${MAP_POINTS_UNPLACED === 1 ? "" : "s"} that belong to no place.</span>`
    : "";
  el.innerHTML = `One dot per geotagged photo, coloured by the place it belongs to. ${tiles}${strays}`;
}
function updateMapStats() {
  const total = MAP_CLUSTERS.reduce((sum, cluster) => sum + cluster.count, 0);
  const named = MAP_CLUSTERS.filter(cluster => cluster.name && cluster.name.trim()).length;
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value.toLocaleString(); };
  set("map-photo-count", total); set("map-place-count", MAP_CLUSTERS.length); set("map-named-count", named);
  // The backend hides tiny one-off clusters (< 10 files, unless named/pinned) so
  // "Places" isn't dominated by single stray photos. Say so, only when it applies.
  const note = document.getElementById("map-hidden-note");
  if (note) {
    const hiddenPlaces = (MAP_HIDDEN && MAP_HIDDEN.places) || 0;
    if (hiddenPlaces > 0) {
      note.hidden = false;
      note.textContent = hiddenPlaces === 1
        ? "1 one-off spot with fewer than 10 photos isn’t shown as a place."
        : `${hiddenPlaces.toLocaleString()} one-off spots with fewer than 10 photos aren’t shown as places.`;
    } else {
      note.hidden = true;
      note.textContent = "";
    }
  }
}
function collageHTML(ids) {
  if (!ids.length) return `<div style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;font-size:20px">📍</div>`;
  const n = Math.min(ids.length, 4);
  // draggable=false: these sit inside merge-draggable place cards and would
  // otherwise hijack the card drag with their own image payload (faceCollage
  // does the same for person cards).
  const imgs = ids.slice(0, 4).map(id => `<img src="/thumb/${id}" loading="lazy" draggable="false" onerror="this.remove()">`).join("");
  return `<div class="cgrid n${n}">${imgs}</div>`;
}
function placeCollage(ids) {
  ids = (ids || []).filter(Boolean).slice(0, 4);
  if (!ids.length) return `<div class="placecollage"><div class="placeempty">📍</div></div>`;
  return `<div class="placecollage">${collageHTML(ids)}</div>`;
}
function renderPlaceGallery() {
  const wrap = document.getElementById("placegallery"); if (!wrap) return;
  if (!MAP_CLUSTERS.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<div class="place-gallery-head"><h3>Places</h3>
      <span class="muted">Named places first · then most photos</span></div>
    <div class="people" id="placegrid"></div>`;
  const grid = document.getElementById("placegrid");
  [...MAP_CLUSTERS].sort((a, b) => {
    const aUnnamed = !(a.name && a.name.trim()), bUnnamed = !(b.name && b.name.trim());
    return aUnnamed - bUnnamed || b.count - a.count || a.id - b.id;
  }).forEach(place => grid.appendChild(placeCard(place)));
}
function placeCard(place) {
  const card = document.createElement("div"); card.className = "pcard";
  card.onclick = guardCardClick(() => showPlaceFromGallery(place.id));
  const name = place.name ? esc(place.name) : "Name this place";
  card.innerHTML = placeCollage(place.thumb_ids) + `<div class="pmeta">
    <button class="pname ${place.name ? "" : "un"}" type="button">${name}</button>
    <div class="pcount">${place.count.toLocaleString()} photo${place.count === 1 ? "" : "s"}</div></div>`;
  card.querySelector(".pname").onclick = event => {
    event.stopPropagation();
    editPlaceCardName(card, place);
  };
  attachMergeDrag(card, { kind: "place", id: place.id, name: place.name, photos: place.count }, refreshPlacesAfterMerge);
  return card;
}
function showPlaceFromGallery(id) {
  const place = MAP_CLUSTERS.find(cluster => cluster.id === id);
  if (MAP && place) MAP.flyTo([place.lat, place.lon], Math.max(MAP.getZoom(), 14));
  selectPlaceCluster(id);
  document.getElementById("lmap")?.scrollIntoView({ behavior: "smooth", block: "center" });
}
function editPlaceCardName(card, place) {
  const meta = card.querySelector(".pmeta"); if (!meta) return;
  card.onclick = null;
  card.draggable = false;   // don't let an in-progress rename start a merge-drag
  meta.className = "pmeta pmeta-editing";
  meta.innerHTML = `<input value="${esc(place.name || "")}" placeholder="Place name" aria-label="Place name">
    <div class="pcount">${place.count.toLocaleString()} photo${place.count === 1 ? "" : "s"} · Enter or click away to save</div>`;
  const input = meta.querySelector("input");
  input.onclick = event => event.stopPropagation();
  let finished = false;
  input.addEventListener("blur", () => { if (!finished) { finished = true; savePlaceCardName(card, place, input); } });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    if (event.key === "Escape") { finished = true; card.replaceWith(placeCard(place)); }
  });
  input.focus(); input.select();
}
async function savePlaceCardName(card, place, input) {
  const name = input.value.trim();
  if (name === (place.name || "")) { card.replaceWith(placeCard(place)); return; }
  input.disabled = true;
  let result;
  try { result = await jpost("/api/map/cluster/rename", { cluster_id: place.id, name }); }
  catch (error) { result = { error: String(error) }; }
  if (!result || result.error) {
    toast("Couldn’t save the place name.", true); card.replaceWith(placeCard(place)); return;
  }
  place.name = name || null;
  await invalidateMapPoints();   // naming can promote a hidden spot to a real place
  updateMapStats();
  renderPlaceGallery();
  drawMap();
  if (S.mapSel === place.id) selectPlaceCluster(place.id);
}
const PLACE_PAGE_SIZE = 120;
async function selectPlaceCluster(id) {
  S.mapSel = id;
  const side = document.getElementById("mapside");
  const wrap = side && side.closest(".mapwrap");
  if (wrap) wrap.classList.add("has-selection");
  setTimeout(() => { if (MAP) { MAP.invalidateSize(); drawMap(); } }, 0);
  side.innerHTML = `<div class="muted">Loading…</div>`;
  // root was missing here before -- the endpoint requires it and silently
  // failed the request without it (caught, swallowed, no response sent).
  const c = await jget(`/api/map/cluster/${id}?root=${S.arch.id}&limit=${PLACE_PAGE_SIZE}`);
  if (S.mapSel !== id) return; // superseded by a newer click
  if (!c || c.error) { side.innerHTML = `<div class="muted">Place not found.</div>`; return; }
  const safeName = (c.name || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, "&quot;");
  const displayName = c.name ? esc(c.name) : "Name this place";
  side.innerHTML = `
    <div class="mapside-name" id="mapsidename">
      <div class="mapside-title"><button class="person-name-button ${c.name ? "" : "un"}" onclick="editClusterName(${id},'${safeName}')">${displayName}</button>
        <span class="muted">${c.total.toLocaleString()} item${c.total === 1 ? "" : "s"}</span></div>
      <div class="mapside-actions"><button class="close-side" onclick="closePlaceCluster()" aria-label="Close place">×</button></div>
    </div>
    ${mergesPanel(c.merges, "place")}
    <div class="grid" id="mapsidegrid" style="grid-template-columns:repeat(auto-fill,minmax(80px,1fr))"></div>
    <div class="infinite-status" id="mapside-sentinel" aria-live="polite"></div>`;
  let firstPage = c.members;
  startInfiniteList("placeList", {
    sentinelId: "mapside-sentinel", pageSize: PLACE_PAGE_SIZE, root: side,
    fetchPage: async offset => {
      if (firstPage) { const page = firstPage; firstPage = null; return page; }
      const res = await jget(`/api/map/cluster/${id}?root=${S.arch.id}&offset=${offset}&limit=${PLACE_PAGE_SIZE}`);
      return (res && res.members) || [];
    },
    onPage: (items, { first }) => {
      const grid = document.getElementById("mapsidegrid");
      if (first) grid.replaceChildren();
      items.forEach(it => grid.appendChild(tile(it)));
    },
  });
}
export function editClusterName(id, current) {
  const box = document.getElementById("mapsidename");
  box.innerHTML = `<div class="inline-name-editor"><input id="mapsidenameinput" value="${esc(current)}" placeholder="e.g. Grandma’s house" aria-label="Place name"></div>`;
  const input = document.getElementById("mapsidenameinput"); input.focus(); input.select();
  let finished = false;
  input.addEventListener("blur", () => { if (!finished) { finished = true; saveClusterName(id, input); } });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { finished = true; selectPlaceCluster(id); }
  });
}
async function saveClusterName(id, input) {
  const name = input.value.trim(); input.disabled = true;
  const result = await jpost("/api/map/cluster/rename", { cluster_id: id, name });
  if (!result || result.error) { toast("Couldn’t save the place name.", true); selectPlaceCluster(id); return; }
  const mc = MAP_CLUSTERS.find(c => c.id === id); if (mc) mc.name = name || null;
  await invalidateMapPoints();   // naming can promote a hidden spot to a real place
  updateMapStats();
  renderPlaceGallery();
  drawMap();
  selectPlaceCluster(id);
}
export function closePlaceCluster() {
  S.mapSel = null;
  const wrap = document.querySelector(".mapwrap"); if (wrap) wrap.classList.remove("has-selection");
  const side = document.getElementById("mapside"); if (side) side.innerHTML = "";
  setTimeout(() => { if (MAP) { MAP.invalidateSize(); drawMap(); } }, 0);
}
// Re-pull the whole cluster list after a place merge (or its undo) rather than
// patching MAP_CLUSTERS in place: a merge can change member_count, the survivor's
// centroid (weighted mean, unless pinned), and which clusters exist at all, and
// the /api/map/clusters?root= floor on "hidden one-off spots" (map-hidden-note)
// needs to be recomputed the same way too. `survivor` is the merged place object
// the backend returned ({id, name, count}); pass it so the side panel can follow
// the merge to wherever the dragged/dropped place actually landed -- the backend
// picks the surviving id by its own named/pinned/count/id chain, which does not
// always match the drop TARGET card runMerge sent as `a`.
export async function refreshPlacesAfterMerge(survivor) {
  const { clusters, hidden } = await jget("/api/map/clusters?root=" + S.arch.id);
  MAP_CLUSTERS = clusters;
  MAP_HIDDEN = hidden || {};
  await invalidateMapPoints();
  updateMapStats();
  renderPlaceGallery();
  drawMap();
  renderMapViewNote();
  if (S.mapSel == null) return;
  const stillOpen = MAP_CLUSTERS.some(c => c.id === S.mapSel);
  if (stillOpen) selectPlaceCluster(S.mapSel);
  else if (survivor) selectPlaceCluster(survivor.id);   // the open place was absorbed -- follow it
}
export function drawMap() {
  if (!MAP) return;
  if (MAP_LAYER) { MAP.removeLayer(MAP_LAYER); MAP_LAYER = null; }
  if (S.mapView === "photos" && MAP_POINTS) { showPhotoPoints(); return; }
  hidePhotoPoints();
  MAP_LAYER = L.layerGroup().addTo(MAP);
  const R = 54; // px screen-bucket radius -- groups nearby PLACES at low zoom
  const bounds = MAP.getBounds(), buckets = {};
  MAP_CLUSTERS.forEach(c => {
    if (!bounds.contains([c.lat, c.lon])) return;
    const pt = MAP.latLngToContainerPoint([c.lat, c.lon]);
    const key = Math.round(pt.x / R) + "_" + Math.round(pt.y / R);
    (buckets[key] = buckets[key] || []).push(c);
  });
  Object.values(buckets).forEach(grp => {
    const lat = grp.reduce((a, c) => a + c.lat, 0) / grp.length;
    const lon = grp.reduce((a, c) => a + c.lon, 0) / grp.length;
    if (grp.length === 1) {
      const c = grp[0];
      const badge = c.count > c.thumb_ids.length ? `<span class="mk-badge">${c.count > 999 ? "999+" : c.count}</span>` : "";
      const icon = L.divIcon({
        className: "", iconSize: [46, 46], html:
          `<div class="mk${c.id === S.mapSel ? " mk-sel" : ""}">${collageHTML(c.thumb_ids)}${badge}</div>`
      });
      const mk = L.marker([lat, lon], { icon }).addTo(MAP_LAYER).on("click", () => selectPlaceCluster(c.id));
      mk.bindTooltip(c.name || "Name this place", { direction: "top", offset: [0, -24], opacity: 0.95 });
    } else {
      const total = grp.reduce((a, c) => a + c.count, 0);
      const rep = grp[0].thumb_ids[0];
      const icon = L.divIcon({
        className: "", iconSize: [52, 52], html:
          `<div class="mk mk-cluster">${rep ? `<img src="/thumb/${rep}" onerror="this.remove()">` : ""}<span>${total.toLocaleString()}</span></div>`
      });
      L.marker([lat, lon], { icon }).addTo(MAP_LAYER).on("click", () => {
        MAP.flyToBounds(L.latLngBounds(grp.map(c => [c.lat, c.lon])), { padding: [60, 60], maxZoom: 16 });
      });
    }
  });
}
// One hue per place, from its id: no palette to run out of, and a place
// keeps its colour between redraws and sessions. The golden-angle step is
// what keeps consecutive ids visibly different instead of a slow gradient.
function pointColour(clusterId) {
  if (!clusterId) return "#98a1ae";      // belongs to no shown place
  return `hsl(${(clusterId * 137.508) % 360}, 68%, 55%)`;
}
// Every photo as its own translucent dot, so the true spread of a place is
// visible (and overlapping shots pile up into a denser blob). Drawn on ONE
// canvas rather than as DOM markers: tens of thousands of divs would lock
// the page up.
//
// Unlike the clustered path this does NOT rebuild on every pan. Building
// ~16k Leaflet layers costs ~0.5s, while the canvas renderer redraws the
// same 16k circles on a move in ~80ms all by itself -- so the layer is
// built once and only thrown away when something it actually depends on
// changes (the points, the open place, or the zoom-derived dot size).
function showPhotoPoints() {
  const zoom = MAP.getZoom();
  // A little bigger as you zoom in: dense at world view (where a city is a
  // few pixels), individually clickable once you're over a street. Bucketed
  // so ordinary zooming doesn't trigger a rebuild.
  const radius = zoom >= 15 ? 6 : zoom >= 11 ? 4.5 : 3.5;
  // With a place open, its photos stay solid and the rest recede, so
  // clicking a place card means the same thing in both views.
  const sel = S.mapSel || 0;
  const built = MAP_POINT_BUILT;
  if (MAP_POINT_LAYER && built && built.points === MAP_POINTS
      && built.sel === sel && built.radius === radius) {
    if (!MAP.hasLayer(MAP_POINT_LAYER)) MAP_POINT_LAYER.addTo(MAP);
    return;
  }
  hidePhotoPoints();
  if (!MAP_POINT_CANVAS) MAP_POINT_CANVAS = L.canvas({ padding: 0.2 });
  const layer = L.layerGroup();
  MAP_POINTS.forEach(([lat, lon, cid, fileId]) => {
    L.circleMarker([lat, lon], {
      renderer: MAP_POINT_CANVAS, radius, weight: 0,
      fillColor: pointColour(cid),
      fillOpacity: !sel ? 0.55 : (cid === sel ? 0.85 : 0.15),
    }).addTo(layer).on("click", () => openItem(fileId));
  });
  layer.addTo(MAP);
  MAP_POINT_LAYER = layer;
  MAP_POINT_BUILT = { points: MAP_POINTS, sel, radius };
}
function hidePhotoPoints() {
  if (MAP_POINT_LAYER && MAP) MAP.removeLayer(MAP_POINT_LAYER);
  MAP_POINT_LAYER = null;
  MAP_POINT_BUILT = null;
}
