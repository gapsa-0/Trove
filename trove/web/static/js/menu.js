// The overflow menu on a card: the few things you can do to a whole group.
//
// Two constraints shape it, and the first one is why this is not a <details>
// like the other popovers here.
//
// **The panel cannot live inside the card.** A card clips its own overflow to
// round the thumbnail (`.pcard { overflow: hidden }`), and lifts on hover with
// a transform, which makes it a stacking context. A panel nested inside is
// therefore cut to a 145px box AND painted under the neighbouring cards. So the
// panel is appended to the document and positioned against the trigger's
// rectangle, which is the only way out of both at once.
//
// **The trigger belongs in the meta row, not on the picture.** These cards are
// photographs first. A translucent chip floating over a face is a sticker on
// the image; a quiet mark on the line that already carries the name and the
// count is part of the card. It also removes the hover-reveal, which on a
// touch screen was a control that only appeared for people who could not
// summon it.

import {
  esc,
} from "./dom.js";

let OPEN = null;   // the one panel on screen, if any

/* Append a trigger to `host` (a card's meta row, or a detail page's action
   slot). `items` is `[{ label, onPick, danger, submenu }]`.

   `submenu` replaces the panel's contents instead of acting and closing --
   "Merge with…" has to ask which one before it can do anything, and the list
   is too long to carry until it is asked for. */
export function cardMenu(host, items, { label = "More actions" } = {}) {
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "cardmenu-trigger";
  trigger.dataset.tip = label;
  trigger.setAttribute("aria-label", label);
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.innerHTML =
    "<svg viewBox='0 0 20 20' aria-hidden='true'><circle cx='4' cy='10' r='1.5'/>"
    + "<circle cx='10' cy='10' r='1.5'/><circle cx='16' cy='10' r='1.5'/></svg>";
  trigger.onclick = event => {
    // The card underneath is a click target of its own; opening the menu must
    // not also open the group it belongs to.
    event.stopPropagation();
    event.preventDefault();
    if (OPEN && OPEN.trigger === trigger) { closeMenu(); return; }
    openMenu(trigger, items, label);
  };
  host.appendChild(trigger);
  return trigger;
}

function openMenu(trigger, items, label) {
  closeMenu();
  const panel = document.createElement("div");
  panel.className = "cardmenu-panel";
  panel.setAttribute("role", "menu");
  panel.setAttribute("aria-label", esc(label));
  fillMenu(panel, items);
  document.body.appendChild(panel);
  OPEN = { panel, trigger };
  trigger.setAttribute("aria-expanded", "true");
  place(panel, trigger);
  // Scrolling the SCREEN would leave a fixed panel behind where the card no
  // longer is, so the menu closes rather than chasing it. Scrolling the panel
  // is the opposite of that and must not: capture on the window sees a scroll
  // of any element, the panel's own included, so "Merge with…" -- the one list
  // here long enough to need scrolling -- shut itself the moment you tried,
  // and only the first few names were ever reachable.
  const onScroll = event => { if (!OPEN.panel.contains(event.target)) closeMenu(); };
  window.addEventListener("scroll", onScroll, { capture: true });
  window.addEventListener("resize", closeMenu, { once: true });
  OPEN.onScroll = onScroll;
}

function fillMenu(panel, items) {
  panel.replaceChildren();
  items.forEach(item => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cardmenu-item" + (item.danger ? " is-danger" : "");
    button.setAttribute("role", "menuitem");
    button.textContent = item.label;
    button.onclick = event => {
      event.stopPropagation();
      if (item.submenu) {
        // Handed the panel itself and a way to dismiss it: a submenu builder
        // fills this same surface, and the panel is re-placed afterwards
        // because a list of names is a different height from three items.
        const anchor = OPEN.trigger;
        Promise.resolve(item.submenu(panel, closeMenu)).then(() => place(panel, anchor));
        return;
      }
      closeMenu();
      item.onPick();
    };
    panel.appendChild(button);
  });
}

/* Under the trigger, right-aligned, flipped up or nudged in when the viewport
   would cut it off -- a card near the bottom of the grid is the common case,
   not the exception. */
function place(panel, trigger) {
  const r = trigger.getBoundingClientRect();
  const box = panel.getBoundingClientRect();
  const margin = 8;
  let top = r.bottom + 6;
  if (top + box.height > window.innerHeight - margin) {
    top = Math.max(margin, r.top - box.height - 6);
  }
  let left = r.right - box.width;
  left = Math.min(Math.max(margin, left), window.innerWidth - box.width - margin);
  panel.style.top = `${Math.round(top)}px`;
  panel.style.left = `${Math.round(left)}px`;
}

export function closeMenu() {
  if (!OPEN) return;
  // The scroll listener outlives `once` now that it can decline to act, so it
  // is taken off with the panel it belongs to.
  window.removeEventListener("scroll", OPEN.onScroll, { capture: true });
  OPEN.panel.remove();
  OPEN.trigger.removeAttribute("aria-expanded");
  OPEN = null;
}

document.addEventListener("pointerdown", event => {
  if (OPEN && !OPEN.panel.contains(event.target) && !OPEN.trigger.contains(event.target))
    closeMenu();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeMenu();
});
