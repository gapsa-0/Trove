// The video stage: what a <video> on the viewer's stage is wired to notice, the
// transport drawn for a re-encoded one, and what is shown when no picture
// arrives at all.
//
// Split from item.js because it is the one part of the viewer that is about a
// *format* rather than about an item: everything here exists because the player
// built into this window reads a short list of containers and a shelf of family
// video is full of the rest. The rest of the viewer -- moving between items,
// zoom, the chrome, the inspector and everything editable on it -- is next door
// and does not care how the picture got there.
//
// item.js imports this for the stage it builds; this imports item.js for three
// facts about the item on screen. That is a cycle, and a deliberate one, the
// same arrangement library.js and results.js have: everything crossing it is a
// hoisted function declaration, defined before any of it runs.

import {
  esc, toast,
} from "./dom.js";
import {
  fileKind, stillOpen, viewer,
} from "./item.js";

/* What the stage is currently holding open, released before the next file
   takes its place. Set by videoStage; a no-op for everything else.

   Detaching a <video> is not the same as stopping it. Chromium keeps the load
   in flight for a while after the element leaves the document, and for a
   re-encoded video that load is an ffmpeg process: arrowing through a folder
   of .avi files left one running per file passed, each holding most of a core,
   and four or five of those is a machine that has stopped responding. Aborting
   the request is what closes the socket, which closes the stream, which kills
   the encoder -- and that has to be asked for. */
const NOTHING_TO_RELEASE = () => {};
let release = NOTHING_TO_RELEASE;
/* Let go of whatever the stage holds. Called before the next file is drawn and
   again when the viewer closes, by which time it is usually already a no-op. */
export function releaseStage() {
  release();
}
/* Stop a media element now: no more events from it, no more bytes for it.
   `removeAttribute` before `load()` is the part that matters -- load() on an
   element with no source is what aborts the request already in flight. */
function stopLoading(el) {
  try {
    el.pause();
    el.removeAttribute("src");
    el.load();
  } catch {
    // A detached or already-torn-down element. Nothing left to stop.
  }
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
export function videoStage(it) {
  const v = document.createElement("video"), id = it.id;
  // Everything this element goes on to attach lives on one signal, so putting
  // the next file on the stage takes all of it off in one call. See
  // releaseStage: the listeners here outlive the element, because two of them
  // are on the stage itself and the stage is not rebuilt between files.
  const alive = new AbortController();
  const on = (target, event, fn) =>
    target.addEventListener(event, fn, { signal: alive.signal });
  release = () => {
    alive.abort();
    stopLoading(v);
    release = NOTHING_TO_RELEASE;
  };
  // Where the stream currently on the element starts, in seconds into the
  // original. Zero for a file playing as itself; whatever was last seeked to
  // for a re-encoded one, which is the whole of what the transport below adds.
  let from = 0;
  const convert = at => {
    from = at;
    // Re-encoding takes about a second to put a first frame up, and a seek
    // pays it again because a pipe cannot be rewound. Unsaid, that second is a
    // still picture that has stopped responding; said, it is a wait somebody
    // can see the end of. Cleared by the first frame, or by the panel if none
    // arrives.
    showLoading();
    v.src = `/file/${id}?play=1${at ? `&t=${at.toFixed(3)}` : ""}`;
    v.load();
    // Rejected when the window will not autoplay -- nothing to report, the
    // transport's own button is right there.
    v.play().catch(() => {});
    mountTransport(v, it, () => from, convert, alive.signal);
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
  on(v, "error", () => nothingDrawn("refused"));
  on(v, "loadedmetadata", () => { if (!v.videoWidth) nothingDrawn("opened"); });
  // stillOpen for the same reason as nothingDrawn: this fires on the file that
  // was abandoned as readily as on the one on screen, and unguarded it took
  // down the note belonging to whatever had replaced it.
  on(v, "loadeddata", () => { if (v.videoWidth && stillOpen(id)) clearLoading(); });
  v.controls = true;
  v.autoplay = true;
  // The frame already extracted for the grid, standing in until the first real
  // one arrives. Without it the element is its own default 300x150 of black --
  // a small dark box adrift on the stage, at the wrong size and the wrong
  // shape, for the whole of the wait. A file with no frame answers 404 and the
  // element falls back to that default, which is where it started.
  v.poster = "/thumb/" + id;
  // ...and the size the catalogue measured, so the picture opens at the size
  // it will keep. The element is otherwise 300x150 until metadata lands and
  // then jumps, taking the transport -- which is laid on the picture's own
  // edges -- across the stage with it. Set as attributes rather than styles
  // because that is what gives the element an aspect ratio to scale within.
  if (it.meta && it.meta.width && it.meta.height) {
    v.width = it.meta.width;
    v.height = it.meta.height;
  }
  v.src = "/file/" + id;
  return v;
}
/* "This is coming", while it is.

   The word is "Loading" and not "Converting", though converting is what is
   happening. Two reasons, and the second is the one that settles it:

   * Which videos need re-encoding is a fact about the decoders in this window,
     not about this person's archive. They opened a video; it is loading. The
     rest of the app says "Loading…" for every other wait and this is not a
     different kind of wait to the one waiting.
   * "Converting" reads as though the file is being changed. Nothing is: the
     re-encoding is a stream that exists for as long as it is watched and is
     never written anywhere. In an app whose promise is that it catalogues
     originals where they lie and alters nothing, that is the last thing to
     imply over somebody's only copy of a video.

   Deliberately only on the re-encoding path. A file that plays as itself is
   the browser's own business and it is quick about it; a spinner flashed over
   every video would be noise on the six thousand that never wait. */
function showLoading() {
  const m = document.getElementById("mmedia");
  if (!m || m.querySelector(".vxwait")) return;
  const note = document.createElement("div");
  note.className = "vxwait";
  note.innerHTML = `<span class="spin"></span>Loading…`;
  m.appendChild(note);
}
function clearLoading() {
  const note = document.querySelector("#mmedia .vxwait");
  if (note) note.remove();
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
   a scrub bar that cannot scrub. So the rest is rebuilt, and rebuilt as a copy
   of what it replaces rather than as something of our own: same two rows, same
   order, same 4px track inset 16px with the buffered stretch behind the played
   one, same 48px between the controls on the right, same icons, same fade-out
   while a video plays untouched. The measurements are off a screenshot of the
   real thing, not off a memory of it.

   Copied because half the archive's video plays as itself and keeps the native
   controls, and a viewer that hands you two different players depending on how
   a file happens to be stored is telling you about our implementation. Nobody
   watching a video needs to know which of the two they got. */
const VX_ICON = {
  play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><path d="M7 5h3.2v14H7zM13.8 5H17v14h-3.2z"/></svg>',
  loud: '<svg viewBox="0 0 24 24"><path d="M4 9.5v5h3.5L12 18V6L7.5 9.5H4z"/>'
    + '<path class="wave" d="M15.5 9a4.2 4.2 0 0 1 0 6M18 6.6a7.6 7.6 0 0 1 0 10.8"/></svg>',
  muted: '<svg viewBox="0 0 24 24"><path d="M4 9.5v5h3.5L12 18V6L7.5 9.5H4z"/>'
    + '<path class="wave" d="m15.5 10 5 4M20.5 10l-5 4"/></svg>',
  full: '<svg viewBox="0 0 24 24"><path class="wave" d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg>',
  more: '<svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.6"/>'
    + '<circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>',
};
const VX_RATES = [0.5, 1, 1.5, 2];

function mountTransport(v, it, from, seek, alive) {
  const m = document.getElementById("mmedia");
  const total = (it.meta && it.meta.duration_s) || 0;
  if (!m || m.querySelector(".vxport")) return;      // a seek, not a first play
  v.controls = false;
  const bar = document.createElement("div");
  bar.className = "vxport";
  bar.innerHTML = `<div class="vxrow">
      <button class="vxplay" type="button" aria-label="Play"></button>
      <span class="vxtime"></span>
      <span class="vxgap"></span>
      <span class="vxsound">
        <button class="vxmute" type="button" aria-label="Mute"></button>
        <input class="vxvol" type="range" min="0" max="1" step="0.02" aria-label="Volume">
      </span>
      <button class="vxfull" type="button" aria-label="Full screen">${VX_ICON.full}</button>
      <button class="vxmore" type="button" aria-label="More" aria-expanded="false"
        aria-haspopup="menu">${VX_ICON.more}</button>
      <div class="vxmenu" role="menu" hidden>
        ${VX_RATES.map(r => `<button type="button" role="menuitemradio" data-rate="${r}">
          ${r === 1 ? "Normal speed" : `${r}× speed`}</button>`).join("")}
        <button type="button" role="menuitem" class="vxpip">Picture in picture</button>
      </div>
    </div>
    <div class="vxbar"${total ? "" : " hidden"}><div class="vxbuf"></div><div class="vxfill"></div>
      <div class="vxknob"></div></div>`;
  const find = c => bar.querySelector("." + c);
  const [play, track, time] = ["vxplay", "vxbar", "vxtime"].map(find);
  const [mute, vol, full] = ["vxmute", "vxvol", "vxfull"].map(find);
  const [more, menu, fill, buf] = ["vxmore", "vxmenu", "vxfill", "vxbuf"].map(find);
  const knob = find("vxknob");
  // Where the finger is while the track is being dragged, in seconds, or null
  // when it is not. Held apart from the video's own position because the two
  // genuinely differ for as long as a drag lasts: the bar has to follow the
  // finger immediately, and the video cannot -- every seek is a second of
  // re-encoding, so committing one per pointermove would queue a hundred
  // encoders to answer a gesture that ends somewhere else entirely.
  let scrubbing = null;
  const paint = () => {
    const at = scrubbing ?? from() + v.currentTime;
    play.innerHTML = v.paused ? VX_ICON.play : VX_ICON.pause;
    play.setAttribute("aria-label", v.paused ? "Play" : "Pause");
    if (total) {
      const played = Math.min(100, (100 * at) / total);
      fill.style.width = `${played}%`;
      knob.style.left = `${played}%`;
      // What has been encoded and arrived, which for a stream is the part you
      // can go back over without paying for a fresh one. The native control
      // draws exactly this, in exactly this grey.
      const ready = v.buffered.length ? from() + v.buffered.end(v.buffered.length - 1) : at;
      buf.style.width = `${Math.min(100, (100 * ready) / total)}%`;
    }
    time.textContent = total ? `${clock(at)} / ${clock(total)}` : clock(at);
    const off = v.muted || !v.volume;
    mute.innerHTML = off ? VX_ICON.muted : VX_ICON.loud;
    mute.setAttribute("aria-label", off ? "Unmute" : "Mute");
    vol.value = String(off ? 0 : v.volume);
    menu.querySelectorAll("[data-rate]").forEach(b =>
      b.setAttribute("aria-checked", String(Number(b.dataset.rate) === v.playbackRate)));
  };
  play.addEventListener("click", () => { v.paused ? v.play().catch(() => {}) : v.pause(); paint(); });
  ["timeupdate", "play", "pause", "volumechange", "progress", "ratechange"]
    .forEach(e => v.addEventListener(e, paint));
  /* Drag, not just click. The native bar can be dragged and this could not,
     which is the one thing left that made it feel like a different control.

     The seek lands on release. During the drag the bar and the readout follow
     the pointer off `scrubbing` while the picture stays where it was, so the
     gesture is answered at once and the expensive part happens once, on the
     position actually chosen. A click is the degenerate drag -- press and
     release in one place -- and takes the same path. */
  const atPointer = event => {
    const box = track.getBoundingClientRect();
    return Math.max(0, Math.min(total, (total * (event.clientX - box.left)) / box.width));
  };
  track.addEventListener("pointerdown", event => {
    if (!total || event.button !== 0) return;
    event.preventDefault();
    scrubbing = atPointer(event);
    track.classList.add("scrubbing");
    paint();
    // Capture keeps the pointer's events coming to the track once it wanders
    // off it, which it will: the bar is four pixels of ink and a drag is a
    // gesture across a window. Refused for a pointer the browser has no record
    // of, and that is survivable rather than fatal -- the window handlers
    // below see the same events either way, which is why they are on the
    // window and not on the track.
    try {
      track.setPointerCapture(event.pointerId);
    } catch {
      // No capture; the drag still runs off the window's own events.
    }
  });
  window.addEventListener("pointermove", event => {
    if (scrubbing === null) return;
    scrubbing = atPointer(event);
    paint();
  }, { signal: alive });
  const stopScrub = () => {
    track.classList.remove("scrubbing");
    const at = scrubbing;
    // Cleared before seeking, or the bar sits at the drop point while the new
    // stream starts somewhere near it, and then jumps.
    scrubbing = null;
    return at;
  };
  window.addEventListener("pointerup", () => {
    const at = stopScrub();
    if (at !== null) seek(at);
  }, { signal: alive });
  window.addEventListener("pointercancel", () => {
    stopScrub();
    paint();
  }, { signal: alive });
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
  const showMenu = open => {
    menu.hidden = !open;
    more.setAttribute("aria-expanded", String(open));
  };
  more.addEventListener("click", () => showMenu(menu.hidden));
  menu.addEventListener("click", event => {
    const hit = event.target.closest("button");
    if (!hit) return;
    if (hit.dataset.rate) v.playbackRate = Number(hit.dataset.rate);
    else if (document.pictureInPictureElement) document.exitPictureInPicture();
    else v.requestPictureInPicture().catch(() => toast("Couldn’t open picture in picture.", true));
    showMenu(false);
  });
  // Anywhere else on the stage closes it, the way every other menu here does.
  // On the stage rather than on the bar, and the stage is the one element here
  // that is *not* rebuilt between files -- so this one needs the signal, where
  // the handlers above go out with the bar they are attached to.
  m.addEventListener("click", event => {
    if (!menu.hidden && !event.target.closest(".vxmore,.vxmenu")) showMenu(false);
  }, { signal: alive });
  mountIdleFade(m, bar, v, alive);
  m.appendChild(bar);
  alignToPicture(m, bar, v, alive);
  paint();
}
/* Sit on the picture, not on the stage.

   The native panel is exactly as wide as the video it belongs to and rests on
   its bottom edge; a 640x480 clip on a dark stage gets a 640-wide bar floating
   where the picture ends. Ours spanned the whole stage, which was the one
   difference left that gave away which of the two players you had -- most
   visible arrowing from an .mp4 straight onto an .avi, where the bar jumped
   the width of the window.

   Measured off the element rather than computed from the aspect ratio: the
   stage sizes it with max-width/max-height, so its own box is already the
   drawn picture with no letterboxing inside it. The observer is what keeps
   that true afterwards -- opening the inspector, resizing the window and
   going fullscreen all move the picture, and all of them resize this element
   to say so. */
function alignToPicture(stage, bar, v, alive) {
  const align = () => {
    const box = v.getBoundingClientRect(), frame = stage.getBoundingClientRect();
    if (!box.width) return;                    // no picture yet; CSS holds it
    bar.style.left = `${box.left - frame.left}px`;
    bar.style.width = `${box.width}px`;
    bar.style.right = "auto";
    bar.style.bottom = `${Math.max(0, frame.bottom - box.bottom)}px`;
  };
  const watch = new ResizeObserver(align);
  watch.observe(v);
  alive.addEventListener("abort", () => watch.disconnect());
  align();
}
/* Out of the way while a video is playing and nobody is doing anything, which
   is the behaviour being copied and also the reason it can be a solid bar over
   the picture in the first place. Any movement brings it back; a paused video
   keeps it, because a paused video is one you are about to reach for. */
function mountIdleFade(stage, bar, v, alive) {
  let idle = null;
  const wake = () => {
    bar.classList.remove("idle");
    clearTimeout(idle);
    idle = setTimeout(() => {
      // Not while the track is being dragged: a pointer held still mid-gesture
      // sends no events, and fading the bar out from under it is the one way
      // to lose a seek that was halfway to being chosen.
      if (!v.paused && !bar.querySelector(".vxbar.scrubbing")) bar.classList.add("idle");
    }, 2600);
  };
  // Same as the menu's: these are on the stage, which the next file inherits.
  ["pointermove", "pointerdown", "focusin"].forEach(
    e => stage.addEventListener(e, wake, { signal: alive }));
  ["play", "pause"].forEach(e => v.addEventListener(e, wake, { signal: alive }));
  alive.addEventListener("abort", () => clearTimeout(idle));
  wake();
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
  // Emptying the stage detaches the element; it does not stop it. Releasing
  // first is what ends the request, and with it the encoder behind a
  // re-encoding that was never going to draw anything.
  releaseStage();
  m.innerHTML = "";
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
