// The settings drawer and the appearance control inside it. The theme is
// applied by the bootstrap script in index.html before first paint, so what is
// left here is reading it back, toggling it, and re-tiling the two Leaflet maps
// that cannot re-theme themselves from CSS.

import {
  syncPickerMapTiles,
} from "./item.js";
import {
  syncPlacesMapTiles,
} from "./places.js";
import {
  ICONS,
} from "./state.js";

export function currentTheme() { return document.documentElement.dataset.theme === "dark" ? "dark" : "light"; }
/* What the user chose, as opposed to what is on screen. "system" means no
   choice has been made and the OS decides -- which was reachable only by never
   having touched the control, since a two-state toggle has no way back to it. */
function themeChoice() {
  const saved = localStorage.getItem("archiveTheme");
  return saved === "dark" || saved === "light" ? saved : "system";
}
const prefersDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
export function syncThemeControl() {
  const dark = currentTheme() === "dark";
  const choice = themeChoice();
  // Gear buttons (settings) share the .theme-toggle look but carry no theme
  // icon/label, so skip anything without a .theme-icon so they don't break here.
  document.querySelectorAll(".theme-toggle,.appearance-fab").forEach(button => {
    const icon = button.querySelector(".theme-icon");
    if (!icon) return;
    icon.innerHTML = dark ? ICONS.sun : ICONS.moon;
    const label = button.querySelector(".theme-label");
    if (label) label.textContent = dark ? "Light appearance" : "Dark appearance";
    button.dataset.tip = dark ? "Use light appearance" : "Use dark appearance";
  });
  // The drawer's segmented control says which of the three is chosen, which a
  // button reading "Dark appearance" could not: that label is an instruction in
  // one theme and a description in the other, and it never revealed whether the
  // app was following the system or had been told.
  document.querySelectorAll("[data-theme-choice]").forEach(button => {
    const on = button.dataset.themeChoice === choice;
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
  document.querySelectorAll(".gear-icon").forEach(el => { el.innerHTML = ICONS.settings; });
}
/* Apply a choice: "light", "dark", or "system" to hand it back to the OS. */
export function setTheme(choice) {
  if (choice === "system") localStorage.removeItem("archiveTheme");
  else localStorage.setItem("archiveTheme", choice);
  const dark = choice === "system" ? prefersDark() : choice === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]').content = dark ? "#101014" : "#f5f5f7";
  syncThemeControl();
  syncMapTiles();
}
export function toggleTheme() {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
}
// Following the system means following it as it changes, not only at startup.
window.matchMedia("(prefers-color-scheme: dark)")
  .addEventListener("change", () => { if (themeChoice() === "system") setTheme("system"); });
// Nothing in here is user-configurable any more: semantic search stopped
// needing an API key when the embedding model moved on-device, so the drawer
// is appearance plus a statement of what runs where.
export function openSettings() {
  const d = document.getElementById("settings-drawer"), b = document.getElementById("drawer-backdrop");
  b.classList.add("open"); d.classList.add("open"); d.setAttribute("aria-hidden", "false");
}
export function closeSettings() {
  const d = document.getElementById("settings-drawer"), b = document.getElementById("drawer-backdrop");
  b.classList.remove("open"); d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
}
function syncMapTiles() {
  syncPlacesMapTiles();
  syncPickerMapTiles();
}
