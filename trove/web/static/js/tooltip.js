/* One tooltip, for every control whose label is not already on screen.
 *
 * The app had been leaning on the browser's `title` attribute: about twenty
 * controls carried one, ten of them repeating their own `aria-label` word for
 * word. A native tooltip waits about a second, cannot be themed, is positioned
 * at the pointer rather than at the control, and never appears for a keyboard
 * or a touch screen at all -- so the one thing a bare chevron needs, a name,
 * was the thing hardest to get out of it.
 *
 * A themed one already existed on exactly one element (`.person-token`, built
 * out of `::after` in library.css). That approach cannot generalise: a
 * pseudo-element is trapped inside its host's overflow, and the two places
 * this is needed most -- the collapsed sidebar and the viewer chrome -- are
 * both clipped. So this is one fixed-position node, owned here, moved to
 * whichever control is being pointed at or focused.
 *
 * Usage is `data-tip="…"` on the control. `data-tip-at="right"` puts it beside
 * rather than above, which is what the collapsed rail wants. A control that
 * shows its own label needs none of this: the tooltip is for the ones that
 * cannot.
 *
 * Three native <select> elements keep their `title` on purpose. The browser
 * owns that popup and draws it outside the page, so nothing here can position
 * against it or replace it; a `data-tip` on the same element would be a second
 * tooltip fighting the first.
 */

let node = null;
let showing = null;

function tip() {
  if (!node) {
    node = document.createElement("div");
    node.className = "apptip";
    node.setAttribute("role", "tooltip");
    node.hidden = true;
    document.body.appendChild(node);
  }
  return node;
}

/* Place it against the control, then pull it back inside the window.
 *
 * Measured after the text is in and the node is visible, because a hidden
 * element has no box to measure -- which is why this reads the rect last
 * rather than up front. */
function place(target, at) {
  const el = tip(), r = target.getBoundingClientRect();
  const box = el.getBoundingClientRect();
  const gap = 8;
  let top, left;
  if (at === "right") {
    top = r.top + (r.height - box.height) / 2;
    left = r.right + gap;
    // No room on the right (a rail against the window edge): flip to the left.
    if (left + box.width > window.innerWidth - 4) left = r.left - gap - box.width;
  } else {
    top = r.top - box.height - gap;
    left = r.left + (r.width - box.width) / 2;
    // Nothing above it: sit under the control instead.
    if (top < 4) top = r.bottom + gap;
  }
  el.style.top = `${Math.max(4, Math.min(top, window.innerHeight - box.height - 4))}px`;
  el.style.left = `${Math.max(4, Math.min(left, window.innerWidth - box.width - 4))}px`;
}

function show(target) {
  const words = target.getAttribute("data-tip");
  if (!words) return;
  // A control that is currently showing its own label does not need one. The
  // nav items carry a tip for the collapsed rail, where the words are gone.
  if (target.closest("nav:not(.collapsed) .navitem")) return;
  const el = tip();
  el.textContent = words;
  el.hidden = false;
  el.classList.remove("on");
  place(target, target.getAttribute("data-tip-at"));
  // Next frame, so the transition has a from-state to run out of.
  requestAnimationFrame(() => { if (showing === target) el.classList.add("on"); });
  showing = target;
}

export function hideTip() {
  if (!showing) return;
  showing = null;
  if (node) { node.classList.remove("on"); node.hidden = true; }
}

const owner = e => (e.target && e.target.closest ? e.target.closest("[data-tip]") : null);

// Pointer and keyboard both, which is the whole point: `title` answers only the
// first. Capture phase so a control inside a stopPropagation-happy widget still
// gets one.
document.addEventListener("pointerover", e => {
  const target = owner(e);
  if (target !== showing) { hideTip(); if (target) show(target); }
}, true);
document.addEventListener("pointerdown", hideTip, true);
document.addEventListener("focusin", e => {
  const target = owner(e);
  hideTip();
  // Only for a control reached by keyboard. A click focuses too, and a tooltip
  // that appears on the thing you just pressed is in the way of the result.
  if (target && target.matches(":focus-visible")) show(target);
}, true);
document.addEventListener("focusout", hideTip, true);
document.addEventListener("keydown", e => { if (e.key === "Escape") hideTip(); }, true);
// A tooltip is positioned against a rect that scrolling invalidates, and there
// is nothing useful to say while the page is moving under it.
window.addEventListener("scroll", hideTip, true);
window.addEventListener("resize", hideTip);
