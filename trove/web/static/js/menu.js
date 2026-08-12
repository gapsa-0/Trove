// A small overflow menu, for the actions that belong to one card or one tile.
//
// It exists because those actions have outgrown being buttons. A person card
// can now be hidden for either of two reasons, and two buttons on every card in
// a grid of hundreds is a wall of chrome for something used rarely -- while a
// single button cannot ask which reason.
//
// Built on <details>, like the history popover and the Browse filters, so it
// inherits the open/close behaviour, keyboard operation and the outside-click
// and Escape dismissal main.js runs for everything matching POPOVERS there.

import {
  esc,
} from "./dom.js";

/* Append a menu to `host`.

   `items` is `[{ label, onPick, danger }]`. `label` is what the menu says and
   the only thing the user reads, so it is written as the action ("Not a
   person"), never as the mechanism.

   The trigger stops its own clicks: these menus sit on cards and tiles that
   are themselves click targets, and opening the menu must not also open the
   thing under it. */
export function cardMenu(host, items, { label = "More actions" } = {}) {
  const box = document.createElement("details");
  box.className = "cardmenu histmenu";
  box.innerHTML = `<summary class="cardmenu-trigger" title="${esc(label)}" aria-label="${esc(label)}">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>
    </summary>
    <div class="hist-menu cardmenu-list" role="group" aria-label="${esc(label)}"></div>`;
  const list = box.querySelector(".cardmenu-list");
  items.forEach(item => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cardmenu-item" + (item.danger ? " is-danger" : "");
    button.textContent = item.label;
    button.onclick = event => {
      event.stopPropagation();
      box.removeAttribute("open");
      item.onPick();
    };
    list.appendChild(button);
  });
  box.querySelector("summary").addEventListener("click", e => e.stopPropagation());
  host.appendChild(box);
  return box;
}
