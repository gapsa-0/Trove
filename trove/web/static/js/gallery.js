// What the viewer's arrow keys walk, and how a screen declares it.
//
// The viewer steps through `S.gallery` in the order the ids are in it, so this
// has to be whatever the screen you opened the item FROM is showing: a person's
// photos when you came from a person, a place's when you came from a place. It
// used to be filled in exactly one place (Browse), which is why the arrows
// silently did nothing on every other screen.

import {
  S,
} from "./state.js";

/* `source` is the phrase the position readout ends with -- "142 of 3,481 · in
   María's photos". It names the SET rather than the screen, because what it has
   to tell you is that the arrows are bounded by a person and are not about to
   wander into the rest of the archive. */
export function setGallery(ids, source) {
  S.gallery = ids;
  S.gallerySource = source;
}

/* The same thing read back off a grid of tiles.

   Derived from the DOM rather than tracked in parallel with it: these grids page
   incrementally and replace their children on the first page, so a list kept
   alongside would need the same reset and append rules and would drift out of
   step with what is actually on screen the first time one of them was missed.
   Every tile carries its own `data-file-id` (library.js:tile), so the grid
   already holds the answer, in the order it is displaying it. */
export function galleryFromGrid(gridId, source) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  setGallery([...grid.querySelectorAll("[data-file-id]")].map(el => Number(el.dataset.fileId)), source);
}
