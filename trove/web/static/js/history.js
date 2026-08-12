// What you changed about this person or pet, and how to take it back.
//
// It replaces a panel that sat between a person's name and their photographs:
// a list of past merges, on screen permanently, above the thing it described.
// That put the record of the work on top of the work. Here it is a control in
// the top bar that opens on demand, so the page opens on the faces.
//
// Built on <details>, like the Browse screen's filter menus, which buys the
// open/closed state, keyboard operation and the outside-click and Escape
// dismissal main.js already runs for `.multi-filter[open]` -- its selectors
// name `.histmenu` too rather than this module adding a third pair of
// document listeners.

import {
  jget, jpost,
} from "./api.js";
import {
  esc, toast,
} from "./dom.js";

/* The trigger, for a detail page's top bar. `entity` is "person" or "pet". */
export function historyButton(entity, id, name) {
  return `<details class="histmenu" id="histmenu" data-entity="${esc(entity)}" data-id="${id}"
      data-name="${esc(name || "")}">
    <summary class="hist-trigger" title="Recent changes" aria-label="Recent changes">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></svg>
    </summary>
    <div class="hist-menu" role="group" aria-label="Recent changes"><div class="hist-empty">Loading…</div></div>
  </details>`;
}

/* Wire the trigger. Called once, after the top bar is in the DOM: the list is
   fetched when the menu is first opened rather than with the page, because
   most visits never open it. */
export function mountHistory(onUndone) {
  const box = document.getElementById("histmenu");
  if (!box) return;
  box.addEventListener("toggle", () => {
    if (box.open) loadHistory(box, onUndone);
  });
}

async function loadHistory(box, onUndone) {
  const { entity, id, name } = box.dataset;
  const panel = box.querySelector(".hist-menu");
  let res;
  try {
    res = await jget(`/api/edit-log?entity=${entity}&id=${id}&name=${encodeURIComponent(name)}`);
  } catch { res = null; }
  const entries = (res && res.entries) || [];
  if (!entries.length) {
    panel.innerHTML = '<div class="hist-empty">No changes yet.</div>';
    return;
  }
  panel.innerHTML = entries.map(row).join("");
  panel.querySelectorAll("button[data-undo]").forEach(button => {
    button.onclick = () => undoEntry(Number(button.dataset.undo), box, onUndone);
  });
}

/* One line of history. The wording says what was done, in the same words the
   control that did it used -- "Removed a photo", not "detach". */
function row(entry) {
  const label = describe(entry);
  const undo = entry.undoable
    ? `<button class="linkbtn" type="button" data-undo="${entry.id}">Undo</button>`
    : `<span class="hist-done">${entry.undone ? "Undone" : ""}</span>`;
  return `<div class="hist-row${entry.undone ? " is-undone" : ""}">
    <span class="hist-what">${label}</span>${undo}</div>`;
}

function describe(entry) {
  const d = entry.detail || {};
  const photos = n => `${n.toLocaleString()} photo${n === 1 ? "" : "s"}`;
  switch (entry.action) {
  case "merge":
    return `Merged in ${d.dropped_name ? `“${esc(d.dropped_name)}”` : "an unnamed group"}`
        + (d.photos ? ` · ${photos(d.photos)}` : "");
  case "rename":
    return d.to
      ? `Named “${esc(d.to)}”${d.from ? `, was “${esc(d.from)}”` : ""}`
      : `Name removed${d.from ? `, was “${esc(d.from)}”` : ""}`;
  case "add_photo": return "Added a photo by hand";
  case "remove_photo": return "Removed a photo";
  case "hide": return "Hidden from People";
  case "set_cover": return "Cover photo changed";
  default: return "Changed";
  }
}

async function undoEntry(entryId, box, onUndone) {
  let res;
  try { res = await jpost("/api/edit-log/undo", { entry_id: entryId }); }
  catch (e) { res = { error: String(e) }; }
  if (!res || res.error) {
    toast((res && res.error) ? `Couldn’t undo: ${res.error}` : "Couldn’t undo that.", true);
    return;
  }
  box.removeAttribute("open");
  if (onUndone) onUndone();
}
