/* The people/pets filter: one checkbox menu, two vocabularies.
 *
 * Split out of library.js, which had grown past the asset budget. This is a
 * widget, and it is not Browse's: the Timeline builds the same control from the
 * same functions with a "tl" prefix. What is left in library.js is the grid and
 * the query that fills it.
 */

import {
  applyFilters,
} from "./library.js";
import {
  applyTimelineFilters,
} from "./timeline.js";
import {
  esc,
} from "./dom.js";
import {
  S,
} from "./state.js";

/* The words each kind of group filter uses. One widget, two vocabularies:
   filtering by pet asks the same question of the same shape of data, and
   saying "Anyone" over a list of dogs would be the giveaway that it was
   People's control wearing a different hat. */
const GROUP_FILTERS = {
  people: {
    none: "Anyone", empty: "No people named yet",
    enable: "Name people in People to enable this filter",
    hint: "Selecting more than one person shows media containing everyone selected.",
    together: n => `${n} people together`, all: "Only media containing all selected people",
    // Written out per kind, not built from the kind, so tools/dev/check_handlers.py
    // can still see which function each control calls -- it reads the source,
    // and an interpolated name is invisible to it.
    attr: prefix => `onchange="onPeopleFilterChange('${prefix}')"`,
  },
  pets: {
    none: "Any pet", empty: "No pets named yet",
    enable: "Name pets in Pets to enable this filter",
    hint: "Selecting more than one pet shows media containing all of them.",
    together: n => `${n} pets together`, all: "Only media containing all selected pets",
    attr: prefix => `onchange="onPetsFilterChange('${prefix}')"`,
  },
};
export function groupFilterHTML(prefix, kind, items) {
  const words = GROUP_FILTERS[kind];
  /* Nothing to filter by yet: a disabled control, the same shape as its
     siblings, saying why it cannot help.

     This used to be an inert `<span>` wearing the select's box -- so the filter
     row held a real select with an arrow, a fake select built from <details>
     without one, and two spans that looked like empty text inputs, three
     patterns for one situation side by side. Places already did the honest
     thing: a real, disabled select whose option names the reason. All three
     do it now. */
  if (!items.length)
    return `<select class="fsel" disabled data-tip="${words.enable}">
      <option>${words.empty}</option></select>`;
  return `<details class="multi-filter" id="${prefix}-${kind}-filter">
    <summary class="fsel"><span id="${prefix}-${kind}-label">${words.none}</span></summary>
    <div class="multi-menu">${items.map(p => `<label class="multi-option">
      <input type="checkbox" value="${p.id}" ${words.attr(prefix)}><span>${esc(p.name)}</span>
    </label>`).join("")}
    <div class="multi-help">${words.hint}</div></div>
  </details>`;
}
export function setGroupChecks(prefix, kind, ids) {
  const chosen = new Set(ids.map(String));
  document.querySelectorAll(`#${prefix}-${kind}-filter input[type="checkbox"]`)
    .forEach(input => input.checked = chosen.has(input.value));
}
export function checkedGroups(prefix, kind) {
  return [...document.querySelectorAll(`#${prefix}-${kind}-filter input:checked`)].map(e => e.value);
}
export function clearGroupChecks(prefix, kind) {
  document.querySelectorAll(`#${prefix}-${kind}-filter input:checked`).forEach(e => e.checked = false);
}
export function updateGroupFilterLabel(prefix, kind, items) {
  const words = GROUP_FILTERS[kind];
  const label = document.getElementById(`${prefix}-${kind}-label`); if (!label) return;
  const ids = checkedGroups(prefix, kind),
    names = ids.map(id => (items.find(p => String(p.id) === id) || {}).name).filter(Boolean);
  label.textContent = !names.length ? words.none : names.length === 1 ? names[0] :
    names.length === 2 ? `${names[0]} + ${names[1]}` : words.together(names.length);
  label.closest("summary").dataset.tip = names.length > 1 ? words.all : names.join("");
}
// The People-shaped calls the timeline and the grid already make.
export const peopleFilterHTML = (prefix, people) => groupFilterHTML(prefix, "people", people);
export const checkedPeople = prefix => checkedGroups(prefix, "people");
export const clearPeopleChecks = prefix => clearGroupChecks(prefix, "people");
export const updatePeopleFilterLabel = (prefix, people) =>
  updateGroupFilterLabel(prefix, "people", people);
export function onPetsFilterChange(prefix) {
  if (prefix === "tl") applyTimelineFilters(); else applyFilters();
}
export function onPeopleFilterChange(prefix) {
  if (prefix === "tl") applyTimelineFilters();
  else { if (S.grid) S.grid.inferredPeople = []; applyFilters(); }
}
