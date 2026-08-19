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
   file-count hint line). `className` is for the callers whose input is
   styled -- the two detail pages share `.detail-name-input`. `onSave` is
   given the trimmed value, the input itself -- so it can disable the field
   while the request is in flight -- and which way to step afterwards: 0 for an
   ordinary commit, +1 or -1 when Tab or Shift+Tab ended it. `onStep` is what
   says there IS a next one; without it Tab is left alone.

   Something already named also gets a way to stop being named. Emptying the
   field has always done it, but only if you guessed that it would; making a
   cluster anonymous again is a real thing to want -- a stranger you named by
   mistake, someone you would rather not have listed -- and it deserves to be
   visible rather than discovered. */
export function inlineNameEdit(host, { value = "", label, className = "", after = "", onSave, onCancel, onStep }) {
  host.innerHTML =
    `<input${className ? ` class="${className}"` : ""} value="${esc(value || "")}"
       placeholder="${esc(label)}" aria-label="${esc(label)}">${after}`
    + (value ? '<button class="quietbtn sm name-clear" type="button">Remove name</button>' : "");
  const input = host.querySelector("input");
  // A card is a click target that opens the thing it describes; clicking into
  // the field to position the cursor must not also open it.
  input.onclick = e => e.stopPropagation();
  let finished = false;
  const commit = (value, step = 0) => { finished = true; onSave(value, input, step); };
  input.addEventListener("blur", () => { if (!finished) commit(input.value.trim()); });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { finished = true; onCancel(); }
    // Tab saves this one and opens the next, which is what naming a screenful
    // of strangers is: type, Tab, type, Tab. The browser's own Tab would put
    // the focus on whatever control happened to come next in the card -- the
    // actions menu -- and leave you reaching for the mouse between every name.
    // Only where the caller can say what "next" is; elsewhere Tab still does
    // what Tab does.
    if (e.key === "Tab" && onStep) {
      e.preventDefault();
      commit(input.value.trim(), e.shiftKey ? -1 : 1);
    }
  });
  const clear = host.querySelector(".name-clear");
  // pointerdown, not click: the input's own blur fires first otherwise and
  // commits the unchanged name, so the button is never reached.
  if (clear) {
    clear.addEventListener("pointerdown", e => {
      e.preventDefault(); e.stopPropagation();
      commit("");
    });
  }
  input.focus(); input.select();
  return input;
}
