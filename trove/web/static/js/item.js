// The viewer: what one item shows, how you move between items, and everything
// editable on it -- reassigning a face, adding a manual person or pet tag,
// correcting the date at whatever precision is known, and attaching or creating
// a place. The place picker's own small map is private here;
// syncPickerMapTiles() is the theme switch's only way in.
//
// Two rules run through the whole panel and are worth knowing before editing it:
//
// * **A feature this archive declined produces no section at all.** Not an
//   empty one, not an explanation. The setup panel is where an archive says
//   what it wants; the viewer must not re-open that conversation on every file.
//   `archiveHasFeature` is the gate, the same one Browse uses for its composer.
// * **"Found nothing" and "not looked at yet" are different facts.** The server
//   sends `read`, one flag per stage, and every empty state branches on it. An
//   archive mid-pipeline is mostly the second case, and rendering it as the
//   first claims a finding we do not have.

import {
  jget, qpost,
} from "./api.js";
import {
  drawBoxes, setBoxSource,
} from "./boxes.js";
import {
  esc, toast,
} from "./dom.js";
import {
  relStrip, renderPanel, wordish,
} from "./panel.js";
import {
  MAP_WORLD_BOUNDS, configureMapViewport, replaceMapTiles, themedTileLayer,
} from "./places.js";
import {
  setGallery,
} from "./gallery.js";
import {
  S, TYPE_ICON, archiveHasFeature, typeLabel,
} from "./state.js";

export let MITEM = null;                 // the currently-open item, mutated in place on edit
let RAIL_OPEN = true;                    // the inspector, remembered across items
let RELATED = null;                      // ids fetched by "Show related files", for this item only
/* Where a jump came from, so it can be undone.

   Opening a picture out of "Looks like this" leaves the gallery you were
   walking behind, which is right -- the arrows should then move through the
   similar set you jumped into. But it also means there is no way back to the
   file you were looking at, and no screen behind the viewer to return to. Each
   jump pushes the item and the gallery it was walking; Back pops one. */
let TRAIL = [];

const has = id => archiveHasFeature(S.arch, id);
const viewer = () => document.getElementById("viewer");

export async function openItem(id, opts = {}) {
  // Named archive, not "whichever one is open": the grid is drawn from ?root=,
  // so a tile is clickable before the open-archive POST has necessarily landed.
  const it = await jget(`/api/item/${id}${S.arch ? `?root=${S.arch.id}` : ""}`);
  // An error payload is an object too, so `!it` alone would let a 404 through
  // and open a viewer with no name, no media and an empty panel.
  if (!it || it.error || !it.name) {
    toast("Couldn’t open that file.", true);
    return;
  }
  MITEM = it;
  // Any ordinary open -- a tile, an arrow, the filmstrip -- ends the trail: you
  // are somewhere you navigated to yourself, not somewhere you jumped from.
  if (!opts.keepTrail) TRAIL = [];
  RELATED = opts.related || null;
  renderStage();
  renderInfo();
  renderChrome();
  renderFilmstrip();
  document.getElementById("modal").classList.add("open");
  prefetchNeighbours();
}

/* ---------- moving between items ----------
   The gallery is whatever screen you opened this from, in the order it is on
   screen (S.gallery). Every screen that opens an item fills it, so the arrows
   walk a person's photos when you came from a person and the Browse results
   when you came from Browse. */
function galleryAt() {
  const ids = S.gallery || [];
  return ids.indexOf(MITEM ? MITEM.id : -1);
}
/* Open a file from the "Looks like this" grid: the arrows then walk those
   results, and Back returns to the file they were found for. */
export function openRelated(id) {
  if (MITEM) {
    TRAIL.push({
      id: MITEM.id,
      gallery: (S.gallery || []).slice(),
      source: S.gallerySource,
      related: RELATED,
    });
  }
  // The results become what the arrows walk. The guard is for the degenerate
  // case of a jump with nothing else alongside it: a file should always be in
  // its own gallery, or the viewer reports it as coming from nowhere.
  const ids = (RELATED || []).map(r => r.id);
  if (!ids.includes(id)) ids.push(id);
  setGallery(ids, "in pictures that look alike");
  openItem(id, { keepTrail: true });
}
/* Open another copy of this file from the Duplicates section.

   The arrows then walk the group and nothing else, which is the claim the
   Duplicates screen already makes about its own tiles (dups.js:openDupCopy):
   the group is the set you are comparing, so running off the end of it lands
   you on an unrelated photograph. Back returns to the copy you came from. */
export function openCopy(id) {
  if (!MITEM || id === MITEM.id) return;
  const group = ((MITEM.duplicates && MITEM.duplicates.members) || []).map(m => m.id);
  TRAIL.push({
    id: MITEM.id,
    gallery: (S.gallery || []).slice(),
    source: S.gallerySource,
    related: RELATED,
  });
  setGallery(group.includes(id) ? group : [id], "in this duplicate group");
  openItem(id, { keepTrail: true });
}
export function viewerBack() {
  const previous = TRAIL.pop();
  if (!previous) return;
  setGallery(previous.gallery, previous.source);
  openItem(previous.id, { keepTrail: true, related: previous.related });
}

export function stepItem(delta) {
  const ids = S.gallery || [], at = galleryAt();
  if (at < 0) return;
  const next = ids[at + delta];
  if (next != null) openItem(next);
}
/* The change that makes arrowing feel instant rather than merely possible: the
   next and previous originals are already in the browser's cache by the time
   you ask for them. Images only -- a video or a PDF is far too large to fetch
   on speculation, and both stream on demand anyway. */
function prefetchNeighbours() {
  const ids = S.gallery || [], at = galleryAt();
  if (at < 0) return;
  [ids[at - 1], ids[at + 1]].forEach(id => {
    if (id == null) return;
    const img = new Image();
    img.src = "/thumb/" + id;            // the filmstrip's copy, always cheap
  });
  // The full-size neighbour is worth it for a photo and nothing else.
  if (MITEM && MITEM.type === "image") {
    const next = ids[at + 1];
    if (next != null) { const i = new Image(); i.src = "/file/" + next; }
  }
}

/* ---------- the stage ---------- */
function renderStage() {
  const it = MITEM, m = document.getElementById("mmedia");
  const v = viewer();
  m.className = "stage";
  m.innerHTML = "";
  if (!m.dataset.zoomMounted) { mountZoom(m); m.dataset.zoomMounted = "1"; }
  // Every file opens fit to the frame; see the note on ZOOM.
  ZOOM = { scale: 1, x: 0, y: 0 };
  // A PDF is rendered by the browser's own viewer: it brings a toolbar, a page
  // count, zoom and text selection, so we build none of them. `/file/` already
  // serves it as application/pdf with Accept-Ranges, which is what lets a long
  // document load the pages you reach rather than all of them.
  const old = v.querySelector(".docstage,.noview");
  if (old) old.remove();
  if (isPdf(it)) {
    const box = document.createElement("div");
    box.className = "docstage";
    const f = document.createElement("iframe");
    f.title = it.name;
    f.src = "/file/" + it.id;
    box.appendChild(f);
    // Clicking into the frame is what hands the arrows over; the frame cannot
    // tell us that itself, so the window's blur is the signal (see main.js).
    v.insertBefore(box, m.nextSibling);
    return;
  }
  if (it.type === "document" || it.type === "archive" || it.type === "other") {
    const d = document.createElement("div");
    d.className = "noview";
    d.innerHTML = `<div class="big">${TYPE_ICON[it.type] || "📦"}</div>
      <div class="say">No preview for ${esc(fileKind(it))}</div>
      <div class="sub">${esc(noPreviewReason(it))}</div>
      <a class="iwide" style="width:auto" href="/file/${it.id}" target="_blank">Open in the app that owns it ↗</a>`;
    v.insertBefore(d, m.nextSibling);
    return;
  }
  if (it.type === "image") {
    const img = document.createElement("img");
    img.src = "/file/" + it.id;
    img.alt = it.name;
    m.appendChild(img);
  } else if (it.type === "video") {
    m.appendChild(videoStage(it));
  } else if (it.type === "audio") {
    m.innerHTML = `<div style="padding:40px;text-align:center">
      <div class="ph" style="font-size:60px">🎵</div>
      <audio src="/file/${it.id}" controls autoplay></audio></div>`;
  }
  setBoxSource(m, it);
  drawBoxes();
  renderZoomBar();
}
/* The stage's <video>, wired to notice when it is showing nothing.

   The player is the one built into this window, and it reads a short list of
   formats: the web's own containers and nothing else. A shelf of family video
   is full of formats that are not on that list, and they fail in two different
   ways -- which is why there are two listeners here and not one.

   * **It refuses the file.** .avi and .wmv never open at all; there is no
     reader for either shape of file in this window. `error` fires before
     anything has looked at the video inside.
   * **It opens the file and draws nothing.** The container is one it knows but
     the video within it is not -- Motion JPEG from an old camera's .mov,
     MPEG-4 Part 2 in a .3gp, HEVC from a recent phone. Nothing raises `error`
     for this: the metadata loads, the length is right, the timeline runs, the
     sound plays, and `videoWidth` stays 0. It is the worse of the two to sit
     through, because every part of it says it is working.

   Hence the test is "did a picture arrive", asked of the element itself. Not
   of the file's name, which cannot answer it: three files in the archive this
   was written against are named .avi, are MP4 inside, and play.

   Either way the answer is the same and is not a message: ffmpeg reads all of
   these, Trove already runs it for every video thumbnail, so the file goes
   back out through it and the window is handed something it does play. About
   a second, and then the video runs. The panel is what is left for the files
   ffmpeg cannot read either. */
function videoStage(it) {
  const v = document.createElement("video"), id = it.id;
  // Where the stream currently on the element starts, in seconds into the
  // original. Zero for a file playing as itself; whatever was last seeked to
  // for a re-encoded one, which is the whole of what the transport below adds.
  let from = 0;
  const convert = at => {
    from = at;
    v.src = `/file/${id}?play=1${at ? `&t=${at.toFixed(3)}` : ""}`;
    v.load();
    // Rejected when the window will not autoplay -- nothing to report, the
    // transport's own button is right there.
    v.play().catch(() => {});
    mountTransport(v, it, () => from, convert);
  };
  const nothingDrawn = why => {
    // Only while this file is still the one on screen: metadata for a video
    // arrives well after an arrow press has moved on, and neither the retry
    // nor the panel may land on whatever is open by then.
    if (!stillOpen(id)) return;
    // Whether the re-encoding has already been tried is read off the element
    // rather than kept in a flag beside it. Swapping `src` leaves the load we
    // abandoned free to raise `error` afterwards, and a flag would have that
    // stale event stand for the new load's verdict -- the panel over a video
    // that is about to play perfectly well.
    if (it.can_reencode && !v.src.includes("play=1")) return convert(0);
    showNoPicture(it, why);
  };
  v.addEventListener("error", () => nothingDrawn("refused"));
  v.addEventListener("loadedmetadata", () => { if (!v.videoWidth) nothingDrawn("opened"); });
  v.controls = true;
  v.autoplay = true;
  v.src = "/file/" + id;
  return v;
}
/* The transport for a re-encoded video, and the reason it exists at all.

   ffmpeg's output arrives down a pipe, so the window is handed a video with no
   length and nothing behind the playhead to jump back to: `duration` grows as
   the bytes land and `seekable` stays empty. The native controls drawn over
   that are actively misleading -- a scrub bar that rescales every few seconds
   and will not move to the middle of a film it says is thirty seconds long
   when it is nineteen minutes.

   So for these the native controls come off and this goes on instead, over the
   two facts that are actually known: how long the video really is, which the
   catalogue measured when it indexed the file, and where in it this stream was
   asked to start. Position is `from + currentTime`, and a click seeks by
   asking for a new stream from the new offset -- the only way to move
   backwards in something that cannot be rewound. It costs about a second at
   any offset, because ffmpeg is told to seek before it decodes rather than
   after.

   A video whose length was never measured gets no bar. There is nothing to
   draw one against, and a bar scaled to a guess is worse than none.

   Everything else here is only present because taking the native controls off
   takes *all* of them off -- there is no way to keep a volume slider and drop
   a scrub bar. So the rest of what they offered is rebuilt: sound, mute, and
   fullscreen. A file that plays as itself keeps the native set, and the two
   have to reach the same things or the viewer answers the same question two
   different ways depending on how the file happens to be stored. */
function mountTransport(v, it, from, seek) {
  const m = document.getElementById("mmedia");
  const total = (it.meta && it.meta.duration_s) || 0;
  if (!m || m.querySelector(".vxport")) return;      // a seek, not a first play
  v.controls = false;
  const bar = document.createElement("div");
  bar.className = "vxport";
  bar.innerHTML = `<button class="vxplay" type="button" aria-label="Play"></button>
    <div class="vxbar"${total ? "" : " hidden"}><div class="vxfill"></div></div>
    <span class="vxtime"></span>
    <button class="vxmute" type="button" aria-label="Mute"></button>
    <input class="vxvol" type="range" min="0" max="1" step="0.02" aria-label="Volume">
    <button class="vxfull" type="button" aria-label="Full screen">⛶</button>`;
  const find = c => bar.querySelector("." + c);
  const [play, track, time] = ["vxplay", "vxbar", "vxtime"].map(find);
  const [mute, vol, full] = ["vxmute", "vxvol", "vxfull"].map(find);
  const fill = track.querySelector(".vxfill");
  const paint = () => {
    const at = from() + v.currentTime;
    play.textContent = v.paused ? "▶" : "❚❚";
    play.setAttribute("aria-label", v.paused ? "Play" : "Pause");
    if (total) fill.style.width = `${Math.min(100, 100 * at / total)}%`;
    time.textContent = total ? `${clock(at)} / ${clock(total)}` : clock(at);
    const off = v.muted || !v.volume;
    mute.textContent = off ? "🔇" : "🔊";
    mute.setAttribute("aria-label", off ? "Unmute" : "Mute");
    vol.value = String(off ? 0 : v.volume);
  };
  play.addEventListener("click", () => { v.paused ? v.play().catch(() => {}) : v.pause(); paint(); });
  ["timeupdate", "play", "pause", "volumechange"].forEach(e => v.addEventListener(e, paint));
  track.addEventListener("click", event => {
    const box = track.getBoundingClientRect();
    seek(Math.max(0, Math.min(total, total * (event.clientX - box.left) / box.width)));
  });
  mute.addEventListener("click", () => {
    // Unmuting a video dragged to zero has to give it something audible back,
    // or the button reports sound that is not there.
    v.muted = !v.muted && !!v.volume;
    if (!v.muted && !v.volume) v.volume = 1;
  });
  vol.addEventListener("input", () => { v.volume = Number(vol.value); v.muted = !v.volume; });
  // The stage rather than the video: fullscreen on the element itself would
  // take this bar off the screen, and with the native controls gone that
  // leaves a video with no way to pause it.
  full.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else m.requestFullscreen().catch(() => toast("Couldn’t go full screen.", true));
  });
  m.appendChild(bar);
  paint();
}
const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
/* Say that no picture arrived, in place of the black rectangle that is all the
   element itself has to show for it.

   The frame above the words is the one already extracted for the grid, so it
   costs nothing here and settles the question the message raises: the file
   reads, and this is what is in it. Where there is no frame either -- a
   truncated download, a .swf -- it stands aside for the icon, which is the
   honest shape for a file nothing here could read. */
function showNoPicture(it, why) {
  const m = document.getElementById("mmedia"), v = viewer();
  m.innerHTML = "";                            // stops the download, and any sound
  const d = document.createElement("div");
  d.className = "noview";
  d.innerHTML = `<img class="poster" src="/thumb/${it.id}" alt=""
      onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'big',textContent:'🎞️'}))">
    <div class="say">${esc(noPictureHeading(it, why))}</div>
    <div class="sub">${esc(noPictureReason(it, why))}</div>
    <a class="iwide" style="width:auto" href="/file/${it.id}" target="_blank">Open in the app that owns it ↗</a>`;
  v.insertBefore(d, m.nextSibling);
}
function noPictureHeading(it, why) {
  // Re-encoding was available and was tried, so the format is no longer the
  // story: nothing on this machine could get a picture out of this file.
  if (it.can_reencode) return "Nothing here could read this video";
  return why === "refused" ? `Can’t play ${fileKind(it)} here` : "Can’t play this video here";
}
/* Why, in terms of what was actually attempted. Three states and they send a
   reader to three different places: a file nothing can read is damaged, a
   format this window will not open needs the converter that is missing, and a
   file it opened but could not draw needs that same converter for a different
   reason. Collapsing them into one sentence would make two of the three wrong.

   Naming ffmpeg is deliberate. The desktop build stages its own copy and never
   shows these two lines at all; the person who sees them installed Trove with
   pip, and the name is the difference between an explanation and a dead end. */
function noPictureReason(it, why) {
  if (it.can_reencode)
    return "Trove re-encoded this file with ffmpeg and there was still no picture in it. It is most likely damaged.";
  if (why === "refused")
    return "This window plays a short list of video formats and this file is not one of them. Trove converts the rest as they play, but that needs ffmpeg, which is not installed here.";
  return "This window read the file, but the video inside it is stored in a way it has no reader for. Trove converts these as they play, but that needs ffmpeg, which is not installed here.";
}
function isPdf(it) {
  return (it.rel_path || "").toLowerCase().endsWith(".pdf");
}
function fileKind(it) {
  const ext = ((it.rel_path || "").split(".").pop() || "").toLowerCase();
  return ext && ext.length <= 5 ? `a .${ext} file` : `this ${typeLabel(it.type)}`;
}
/* Why there is nothing to look at, in terms of what was actually read -- which
   depends on whether this archive reads documents at all, and whether the pass
   has reached this file. */
function noPreviewReason(it) {
  if (!has("documents")) return "Nothing here can draw this file, and this archive does not read the text inside documents.";
  if (!it.read.text) return "Nothing here can draw this file. Trove has not read it yet either, so its words are not searchable, for now.";
  if (it.text) return `Nothing here can draw this file. Trove read ${wordish(it.text.chars)} out of it, and they are searchable.`;
  return "Nothing here can draw this file, and there was no text in it to read.";
}
/* ---------- zoom ----------
   Scale 1 is "fit the frame", which is where every file opens; the image is
   laid out by the stage's own max-width/max-height and this only ever scales up
   from there. Everything is one transform on the <img>: anchoring at the
   pointer is then arithmetic rather than scroll juggling, it composites instead
   of relaying out a large bitmap, and the controls have a single number to read.

   Reset on every open, deliberately. Zoom is about the picture in front of you,
   and carrying 400% onto the next file means arrowing through an archive shows
   you a sequence of arbitrary crops. */
const ZOOM_MAX = 8;
let ZOOM = { scale: 1, x: 0, y: 0 };

function stageImg() {
  const m = document.getElementById("mmedia");
  return m ? m.querySelector("img") : null;
}
function resetZoom() {
  ZOOM = { scale: 1, x: 0, y: 0 };
  applyZoom();
}
function applyZoom() {
  const img = stageImg(), m = document.getElementById("mmedia");
  if (img) img.style.transform = `translate(${ZOOM.x}px, ${ZOOM.y}px) scale(${ZOOM.scale})`;
  if (m) m.classList.toggle("zoomed", ZOOM.scale > 1.001);
  renderZoomBar();
  // The boxes are positioned against the image's rendered rectangle, which the
  // transform has just moved.
  drawBoxes();
}
// A slider position of 0..100 mapped onto 1..ZOOM_MAX geometrically, so the
// first half of the travel is the range people actually use.
const sliderToScale = v => Math.pow(ZOOM_MAX, v / 100);
const scaleToSlider = s => Math.round(100 * Math.log(s) / Math.log(ZOOM_MAX));

function renderZoomBar() {
  const bar = document.getElementById("zoombar");
  if (!bar) return;
  // Only once there is something to control. At fit there is nothing to say,
  // and a permanent bar over every photograph is chrome earning nothing.
  const show = MITEM && MITEM.type === "image" && ZOOM.scale > 1.001;
  bar.hidden = !show;
  if (!show) return;
  document.getElementById("zoom-range").value = String(scaleToSlider(ZOOM.scale));
  document.getElementById("zoom-pct").textContent = `${Math.round(ZOOM.scale * 100)}%`;
  document.getElementById("zoom-out").disabled = ZOOM.scale <= 1.001;
  document.getElementById("zoom-in").disabled = ZOOM.scale >= ZOOM_MAX - 0.001;
}

/* Zoom to `scale`, keeping whatever is under (clientX, clientY) where it is.
   With transform-origin at the image's own top-left, the point under the cursor
   sits at (client - rect) in displayed pixels, so holding it still is a shift
   of that distance times the change in scale. */
function zoomTo(scale, clientX, clientY) {
  const img = stageImg();
  if (!img) return;
  const next = Math.max(1, Math.min(ZOOM_MAX, scale));
  const rect = img.getBoundingClientRect();
  const ax = clientX == null ? rect.left + rect.width / 2 : clientX;
  const ay = clientY == null ? rect.top + rect.height / 2 : clientY;
  const k = next / ZOOM.scale;
  ZOOM.x -= (ax - rect.left) * (k - 1);
  ZOOM.y -= (ay - rect.top) * (k - 1);
  ZOOM.scale = next;
  if (ZOOM.scale <= 1.001) { ZOOM.x = 0; ZOOM.y = 0; ZOOM.scale = 1; }
  else clampPan();
  applyZoom();
}
/* Keep at least a quarter of the frame covered by the picture, so it can be
   pushed to the edge to look at a corner but never flicked off screen. */
function clampPan() {
  const img = stageImg(), m = document.getElementById("mmedia");
  if (!img || !m) return;
  const frame = m.getBoundingClientRect();
  const w = img.offsetWidth * ZOOM.scale, h = img.offsetHeight * ZOOM.scale;
  const slackX = Math.max(0, (w - frame.width) / 2) + frame.width * 0.25;
  const slackY = Math.max(0, (h - frame.height) / 2) + frame.height * 0.25;
  ZOOM.x = Math.max(-slackX, Math.min(slackX, ZOOM.x));
  ZOOM.y = Math.max(-slackY, Math.min(slackY, ZOOM.y));
}

export function zoomStep(direction) {
  zoomTo(ZOOM.scale * (direction > 0 ? 1.5 : 1 / 1.5), null, null);
}
export function zoomToSlider(value) {
  zoomTo(sliderToScale(Number(value)), null, null);
}
export function zoomReset() { resetZoom(); }

/* Wheel, trackpad and drag, bound to the stage for the life of one item.
   A trackpad pinch arrives as a wheel event with ctrlKey set, which is why both
   paths lead here; a plain two-finger scroll zooms too, because the stage has
   nothing else to scroll. */
function mountZoom(m) {
  m.addEventListener("wheel", event => {
    if (!MITEM || MITEM.type !== "image") return;
    event.preventDefault();
    // Pinch gestures report far larger deltas than a wheel notch, so the
    // exponent is on the delta itself rather than a fixed step per event.
    const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0022));
    zoomTo(ZOOM.scale * factor, event.clientX, event.clientY);
  }, { passive: false });

  m.addEventListener("pointerdown", event => {
    if (ZOOM.scale <= 1.001 || event.button !== 0) return;
    event.preventDefault();
    m.setPointerCapture(event.pointerId);
    m.classList.add("panning");
    let lastX = event.clientX, lastY = event.clientY;
    const move = e => {
      ZOOM.x += e.clientX - lastX;
      ZOOM.y += e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      clampPan();
      applyZoom();
    };
    const up = () => {
      m.removeEventListener("pointermove", move);
      m.removeEventListener("pointerup", up);
      m.removeEventListener("pointercancel", up);
      m.classList.remove("panning");
    };
    m.addEventListener("pointermove", move);
    m.addEventListener("pointerup", up);
    m.addEventListener("pointercancel", up);
  });

  // Double-click is the shortcut everyone tries: in to 200% at the point you
  // clicked, or back to fit if already in.
  m.addEventListener("dblclick", event => {
    if (!MITEM || MITEM.type !== "image") return;
    if (ZOOM.scale > 1.001) resetZoom();
    else zoomTo(2, event.clientX, event.clientY);
  });
}
/* ---------- floating chrome ---------- */
function renderChrome() {
  const it = MITEM, v = viewer();
  v.classList.toggle("rail-on", RAIL_OPEN);
  const ids = S.gallery || [], at = galleryAt();
  const pos = document.getElementById("vpos");
  // Position only when we know it. Opened from somewhere with no gallery
  // (a map pin, say), the name is the honest thing to show instead of a
  // made-up "1 of 1".
  pos.innerHTML = at >= 0 && ids.length > 1
    ? `<b>${at + 1}</b><span class="sep">of</span><b>${ids.length.toLocaleString()}</b><span class="in">· ${esc(gallerySource())}</span>`
    : `<span class="in">${esc(it.name)}</span>`;
  const back = document.getElementById("vback");
  back.hidden = !TRAIL.length;
  // Two states, not one. At either end of a set the arrow is disabled and stays
  // put -- greyed out, it says there are others and which way they lie, and
  // removing it would shift the other one across the moment you reached the
  // first file. With nowhere to go at all, though -- a search that matched one
  // file, a place with one photo, a file opened from somewhere with no set
  // behind it -- there is no set to be at the end of, and two dead controls are
  // furniture over the picture rather than an answer to anything.
  //
  // Same test the filmstrip already applies to itself (renderFilmstrip), so the
  // three things that report on a set now agree about when there is one: no
  // strip, no arrows, and a readout that gives the file's name instead of a
  // made-up "1 of 1".
  const alone = at < 0 || ids.length < 2;
  const prev = document.getElementById("vprev"), next = document.getElementById("vnext");
  prev.hidden = next.hidden = alone;
  prev.disabled = !(at > 0);
  next.disabled = !(at >= 0 && at < ids.length - 1);
  document.getElementById("vinfo").setAttribute("aria-pressed", String(RAIL_OPEN));
  renderChip();
}
function gallerySource() {
  return S.gallerySource || "in Browse";
}
export function toggleInspector() {
  RAIL_OPEN = !RAIL_OPEN;
  renderChrome();
  // The map is laid out against a panel that just changed width.
  if (MMAP) setTimeout(() => MMAP && MMAP.invalidateSize(), 300);
}
/* The map chip: a geotagged file still says where it was with the inspector
   closed. Drawn from the same coordinates, never its own tile request. */
function renderChip() {
  const chip = document.getElementById("vchip"), it = MITEM;
  const show = it.gps && has("places") && it.place && it.place.name;
  chip.hidden = !show;
  if (show) {
    chip.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></svg>
      <span class="cap">${esc(it.place.name)}</span>`;
  }
}

/* ---------- the filmstrip ---------- */
function renderFilmstrip() {
  const film = document.getElementById("vfilm");
  const ids = S.gallery || [], at = galleryAt();
  // `no-film` is what tells the stage and the inspector to run to the bottom
  // of the frame instead of stopping above a strip that is not there.
  const v = viewer();
  if (at < 0 || ids.length < 2) {
    film.innerHTML = ""; film.hidden = true; v.classList.add("no-film"); return;
  }
  film.hidden = false;
  v.classList.remove("no-film");
  // A window around the current file rather than the whole gallery: a 97k-file
  // archive would otherwise build 97k buttons every time you press an arrow.
  const from = Math.max(0, at - 12), to = Math.min(ids.length, at + 13);
  film.innerHTML = ids.slice(from, to).map((id, n) => {
    const i = from + n;
    // A file with no thumbnail (a .docx, a PDF on an install without the PDF
    // reader) answers 404, and a broken-image glyph in the strip reads as a
    // bug rather than as "nothing to show".
    return `<button type="button" onclick="openItem(${id})" aria-current="${i === at}"
      aria-label="File ${i + 1} of ${ids.length}"><img src="/thumb/${id}" loading="lazy" alt=""
        onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'filmph',textContent:'📄'}))"></button>`;
  }).join("");
  const cur = film.querySelector('[aria-current="true"]');
  if (cur) cur.scrollIntoView({ block: "nearest", inline: "center" });
}

/* ---------- the inspector ---------- */
export function renderInfo() {
  closePick();
  disposeItemMap();
  const it = MITEM;
  if (!it) return;
  document.getElementById("minfo").innerHTML = renderPanel(it, RELATED);
  mountItemMap();
}


/* The three things the panel's controls DO. Their markup is in panel.js, which
   is pure; anything that touches the open item, the clipboard or the network
   belongs here with the rest of the edit flows. */
export function copyText() {
  const t = MITEM && MITEM.text;
  if (!t || !t.transcript) return;
  navigator.clipboard.writeText(t.transcript)
    .then(() => toast("Copied the detected text."))
    .catch(() => toast("Couldn’t copy that text.", true));
}
/* Reveal the file in the OS file manager. Desktop only: a browser tab has no
   way to do this, so the control is not drawn there at all (see panel.js).
   The absolute path comes from the payload, and the main process checks it is
   under a registered archive root before handing it to the shell. */
export function openFileLocation() {
  if (!window.archiveDesktop || !MITEM || !MITEM.abs_path) return;
  window.archiveDesktop.revealFile(MITEM.abs_path)
    .catch(() => toast("Couldn’t open that folder.", true));
}
/* Deliberately on demand, never on open: this is the only thing the panel can
   ask for that costs a pass over every embedding in the archive, and a viewer
   you hold an arrow key down on must not start one per file. */
export function showRelated() {
  const id = MITEM.id, hold = document.getElementById("relhold");
  if (!hold) return;
  hold.innerHTML = `<button class="findbtn" disabled>Looking through the archive…</button>`;
  jget(`/api/similar?id=${id}&limit=8${S.arch ? `&root=${S.arch.id}` : ""}`)
    .then(r => {
      if (!stillOpen(id)) return;
      RELATED = (r && r.items) || [];
      const h = document.getElementById("relhold");
      if (h) h.innerHTML = relStrip(RELATED);
    })
    .catch(() => {
      if (!stillOpen(id)) return;
      const h = document.getElementById("relhold");
      if (h) h.innerHTML = `<div class="imuted">Couldn’t look for related files just now.</div>`;
    });
}

/* ---------- the item's map ----------
   Drawn as a pin immediately and given tiles only once you have stopped on the
   file. Opening an item is a far more frequent act than opening Places, and
   arrowing through two hundred geotagged photos would otherwise stream two
   hundred sets of coordinates to a public tile server -- turning the one
   bounded exception in ARCHITECTURE.md into a side effect of browsing. Flick
   past and nothing is fetched; stop to look and the map fills in. */
const TILE_DWELL_MS = 400;
let MMAP = null, MMAP_TILES = null, MMAP_T = null;
function disposeItemMap() {
  clearTimeout(MMAP_T); MMAP_T = null;
  if (MMAP) { MMAP.remove(); MMAP = null; }
  MMAP_TILES = null;
}
function mountItemMap() {
  const it = MITEM;
  if (!it || !it.gps || !document.getElementById("imap")) return;
  const id = it.id;
  MMAP_T = setTimeout(() => {
    if (!stillOpen(id)) return;
    const host = document.getElementById("imap");
    if (!host) return;
    host.innerHTML = "";
    MMAP = L.map(host, {
      zoomControl: false, attributionControl: false, dragging: false,
      scrollWheelZoom: false, doubleClickZoom: false, boxZoom: false,
      keyboard: false, zoomSnap: 0, maxBounds: MAP_WORLD_BOUNDS, maxBoundsViscosity: 1
    });
    configureMapViewport(MMAP);
    MMAP_TILES = themedTileLayer().addTo(MMAP);
    MMAP.setView([it.gps.lat, it.gps.lon], 14);
    L.circleMarker([it.gps.lat, it.gps.lon], {
      radius: 7, weight: 2, color: "#fff", fillColor: "#3a7bd5", fillOpacity: 1
    }).addTo(MMAP);
    setTimeout(() => MMAP && MMAP.invalidateSize(), 40);
  }, TILE_DWELL_MS);
}
// Optimistic saves: update the panel now, persist in the background, and roll back
// only if the DB write actually fails, so editing feels instant even while the
// pipeline holds the single writer. Every background callback bails out (or re-checks
// stillOpen) if the modal has since closed or moved to another item.
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
  // The editor replaces the whole of #placeval, and on a geotagged file that
  // is where the item's map lives -- so the Leaflet handle has to go with the
  // node it is drawn into, or it survives pointing at a detached element.
  disposeItemMap();
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
  // The item's own map is the second Leaflet handle this module owns, and it
  // cannot re-theme itself from CSS either.
  if (MMAP) MMAP_TILES = replaceMapTiles(MMAP, MMAP_TILES);
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
  closePick();
  disposeItemMap();
  TRAIL = [];
  document.getElementById("modal").classList.remove("open");
  const v = viewer();
  const doc = v.querySelector(".docstage,.noview");
  if (doc) doc.remove();                        // stops a playing PDF/embed holding focus
  document.getElementById("mmedia").innerHTML = "";
  document.getElementById("vfilm").innerHTML = "";
  MITEM = null; RELATED = null;
}
