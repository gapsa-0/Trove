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
export function syncThemeControl() {
  const dark = currentTheme() === "dark";
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
  document.querySelectorAll(".gear-icon").forEach(el => { el.innerHTML = ICONS.settings; });
}
export function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("archiveTheme", next);
  document.querySelector('meta[name="theme-color"]').content = next === "dark" ? "#101014" : "#f5f5f7";
  syncThemeControl();
  syncMapTiles();
}
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
