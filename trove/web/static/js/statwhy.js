// What the figures at the top of a screen actually count.
//
// Four screens open with a row of stat tiles, and their numbers are defined
// against each other: "unique files" only means something once you know what
// "redundant copies" excludes, and "faces" is not "photos with faces" for a
// reason worth one sentence.
//
// So the definitions are revealed as a SET -- pointing at the row brings up all
// of them at once, not the one under the pointer. Read side by side they answer
// each other, which is the actual question being asked; one at a time leaves
// the comparison to be held in the head.
//
// There is no control for it. Everything here is markup that is always present
// and hidden by CSS (see .stat-why in overview.css, which owns the whole
// interaction). Nothing to press, nothing to remember having pressed, and no
// state to carry across a re-render.
//
// The cost is that this needs a hovering pointer: a tile is not focusable, so a
// keyboard cannot reach these sentences, and neither can a touch screen, where
// the rule is switched off outright (@media (hover: hover) in overview.css)
// because a caption that appears on tap and stays until something else is
// tapped is a different, worse control. What is reachable either way is the
// "How this works" button in the same page head, whose page says the same
// things at length -- see web/docs/duplicates.md on "unique files".

// One tile's definition: what the number counts, written as a sentence rather
// than as a description of how it is computed.
//
// Carries the tile's label AND its figure, because it REPLACES the tile: the
// number is underneath it while it is being read, and a definition with
// neither the name nor the value of the thing it defines is a sentence you
// have to match back to a number from memory. The figure is set small here --
// it is what is being explained, not what is being reported.
//
// Kept to two lines at the tile's width. The tile reserves exactly this much
// room (see .stat:has(.stat-why)), so a third line would make the row taller
// the moment a pointer crossed it.
export function why(label, value, text) {
  return `<div class="stat-why">
      <div class="k">${label} <span class="stat-why-v">${value}</span></div>
      <p>${text}</p>
    </div>`;
}

// Update a tile's figure and the copy of it on that tile's definition together.
//
// People and Pets poll while their detect stage runs, and Places fills its
// tiles once the clusters arrive. Writing only to the tile leaves the
// definition quoting whatever the figure was when the screen was drawn, which
// is wrong precisely while someone is watching the number move.
export function setStat(id, html) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = html;
  const echo = el.closest(".stat")?.querySelector(".stat-why-v");
  if (echo) echo.innerHTML = html;
}
