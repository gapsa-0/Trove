// Typing a name over the thing it names.
//
// Four places do this -- a person card, a person's page, a pet card, a pet's
// page -- and all four want the same small contract: an input replaces the
// label in place, Enter and clicking away commit, Escape abandons, and the
// commit happens exactly ONCE however you left the field.
//
// That last clause is why this is a module rather than four copies. `blur`
// fires after Enter too, so a naive pair of listeners posts the same rename
// twice; the `finished` latch below is the whole reason the two existing
// copies were identical line for line. What differs between the four callers
// is only where the input goes and what happens after it saves, so those are
// the arguments.
//
// Pets had none of this until now: their grid name was a plain <div> with no
// handler, and their page called window.prompt(), which the desktop shell
// does not implement -- `prompt` is defined there as a function that throws.

import {
  esc,
} from "./dom.js";

/* Replace `host`'s contents with a name input and hand back the element.

   `after` is markup appended beside the input (the cards use it for the
   photo-count hint line). `className` is for the callers whose input is
   styled -- the two detail pages share `.detail-name-input`. `onSave` is
   given the trimmed value and the input itself, so it can disable the field
   while the request is in flight. */
export function inlineNameEdit(host, { value = "", label, className = "", after = "", onSave, onCancel }) {
  host.innerHTML =
    `<input${className ? ` class="${className}"` : ""} value="${esc(value || "")}"
       placeholder="${esc(label)}" aria-label="${esc(label)}">${after}`;
  const input = host.querySelector("input");
  // A card is a click target that opens the thing it describes; clicking into
  // the field to position the cursor must not also open it.
  input.onclick = e => e.stopPropagation();
  let finished = false;
  input.addEventListener("blur", () => {
    if (finished) return;
    finished = true;
    onSave(input.value.trim(), input);
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { finished = true; onCancel(); }
  });
  input.focus(); input.select();
  return input;
}
