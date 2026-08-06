// Where the faces are, drawn over the photo.
//
// Its own module because two things need it and neither should own it: the
// viewer draws and re-draws the boxes (on open, and on every zoom or pan), and
// the inspector's People section carries the control that turns them on and the
// rows that highlight one at a time. Keeping the state here is what lets
// panel.js ask `boxesShown()` without importing the viewer that imports panel.js.
//
// Boxes are siblings of the <img>, not children of it: the image carries a
// transform for zoom, and a child would be scaled by it -- a 1.5x zoom would
// give a 1.5x-thick border and 1.5x text. Positioning them from the image's
// rendered rectangle instead keeps the frame crisp at any magnification.

import {
  esc,
} from "./dom.js";

let SHOWN = true;      // the "Show on photo" toggle, remembered across items
let HOT = null;        // one face lit up by hovering its row
let HOST = null;       // the stage the boxes are drawn into
let ITEM = null;       // the item whose faces they are

export function boxesShown() { return SHOWN; }

/* Called by the viewer whenever the stage changes what it is showing. */
export function setBoxSource(host, item) {
  HOST = host;
  ITEM = item;
  HOT = null;
}

export function toggleBoxes() {
  SHOWN = !SHOWN;
  drawBoxes();
  // The label lives in the panel, which is not re-rendered for this: repainting
  // the whole inspector would collapse any editor open in it.
  const button = document.getElementById("boxtoggle");
  if (button) button.textContent = SHOWN ? "Hide on photo" : "Show on photo";
}

/* Hovering a face row lights that person's box -- and shows it even when the
   boxes are switched off, which is the one moment you actually want to know
   which of four people in a group shot the row refers to. */
export function highlightFace(faceId) {
  // Coerced: the id can arrive as a number from an inline handler or as a
  // string from a `data-face-id`, and `===` against the payload's number would
  // quietly match neither.
  HOT = faceId == null || faceId === "" ? null : Number(faceId);
  drawBoxes();
}

export function drawBoxes() {
  if (!HOST || !ITEM) return;
  HOST.querySelectorAll(".facebox").forEach(box => box.remove());
  const img = HOST.querySelector("img");
  if (!img || ITEM.type !== "image") return;
  const faces = (ITEM.people || []).filter(f => f.box && (SHOWN || f.face_id === HOT));
  if (!faces.length) return;
  const place = () => {
    HOST.querySelectorAll(".facebox").forEach(box => box.remove());
    const w = img.naturalWidth, h = img.naturalHeight;
    if (!w || !h) return;
    // The rendered rectangle already carries the zoom transform, so a box
    // expressed as a fraction of it lands correctly at any magnification.
    const r = img.getBoundingClientRect(), hr = HOST.getBoundingClientRect();
    faces.forEach(f => {
      const box = document.createElement("div");
      box.className = "facebox" + (f.face_id === HOT ? " hot" : "");
      box.style.left = `${r.left - hr.left + (f.box.x / w) * r.width}px`;
      box.style.top = `${r.top - hr.top + (f.box.y / h) * r.height}px`;
      box.style.width = `${(f.box.w / w) * r.width}px`;
      box.style.height = `${(f.box.h / h) * r.height}px`;
      if (f.name) box.innerHTML = `<span>${esc(f.name)}</span>`;
      HOST.appendChild(box);
    });
  };
  if (img.complete) place(); else img.addEventListener("load", place, { once: true });
}
