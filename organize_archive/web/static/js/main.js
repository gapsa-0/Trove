// The Trove frontend, entry point.
//
// Loaded as `<script type="module">`, so everything in here is module-scoped:
// nothing lands on `window` unless the export block at the bottom puts it there.
// That block is the one thing to read before changing anything above it.

import {
  closeSettings, openSettings, syncThemeControl, toggleTheme,
} from "./settings.js";
import {
  applyHash, applyNavCollapsed, showSection, toPicker, toggleNav,
} from "./router.js";
import {
  ARCHIVES, addArchiveFromForm, loadPicker, openArchive,
} from "./picker.js";

import {
  applyFilters, applySort, clearFilters, onPeopleFilterChange, onYearChange,
} from "./library.js";
import {
  onSemanticComposerInput, onSemanticComposerKeydown, onSemanticComposerPaste,
  semanticSubmit,
} from "./search.js";

import {
  MITEM, addPersonPicker, addPetPicker, closeModal, closePick, editDate, editPlace, newPlace,
  onAddPerson, onAddPet, onPlaceSelect, openItem, reassignFace, removeManualPerson,
  removeManualPet, renderInfo, saveDate, saveNewPlace,
} from "./item.js";
import {
  renamePet,
} from "./pets.js";
import {
  answerSuggest, backToPeople, editPersonName, hidePerson,
} from "./people.js";
import {
  mergeAskCancel, undoMerge,
} from "./merge.js";
import {
  closePlaceCluster, editClusterName, setMapView,
} from "./places.js";
import {
  applyTimelineFilters, clearTimelineFilters, onTimelineYearChange,
} from "./timeline.js";
import {
  setStorageMetric, togglePipelinePause, toggleStagePause,
} from "./overview.js";

import {
  S,
} from "./state.js";

// Checkbox filter menus behave like native popovers: only one stays open, and
// clicking elsewhere or pressing Escape dismisses it without changing choices.
document.addEventListener("pointerdown", event => {
  document.querySelectorAll(".multi-filter[open]").forEach(menu => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape")
    document.querySelectorAll(".multi-filter[open]").forEach(menu => menu.removeAttribute("open"));
});
document.addEventListener("toggle", event => {
  if (event.target.matches && event.target.matches(".multi-filter[open]"))
    document.querySelectorAll(".multi-filter[open]").forEach(menu => {
      if (menu !== event.target) menu.removeAttribute("open");
    });
}, true);

window.addEventListener("hashchange", () => {
  const match = (location.hash || "").match(/#\/archive\/(\d+)\/(\w+)/);
  if (!match) return;
  const archive = ARCHIVES.find(item => item.id === Number(match[1]));
  if (!archive) return;
  if (S.arch && S.arch.id === archive.id) showSection(match[2]);
  else openArchive(archive, match[2]);
});

window.addEventListener("pagehide", () => {
  if (S.arch) navigator.sendBeacon("/api/archive/close", JSON.stringify({ root_id: S.arch.id }));
});

applyNavCollapsed();

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("settings-drawer").classList.contains("open")) {
    closeSettings(); return;
  }
  if (e.key === "Escape") { closeModal(); return; }
  if (!MITEM || !document.getElementById("modal").classList.contains("open")) return;
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const ids = S.gallery || [], at = ids.indexOf(MITEM.id), next = ids[at + (e.key === "ArrowLeft" ? -1 : 1)];
  if (next != null) { e.preventDefault(); openItem(next); }
});

syncThemeControl();
loadPicker().then(applyHash);

// Inline `on*` attributes in index.html -- and in the template literals that the
// screen modules use to generate markup -- are evaluated by the browser against
// `window`, not against any module's scope. Every function named by one of them
// must therefore be re-exported here, or its button silently does nothing when
// clicked, with no error at load time.
// This list is the frontend's public surface; keep it alphabetical.
// `tools/dev/check_handlers.py` fails the build if the two ever disagree.
Object.assign(window, {
  addArchiveFromForm, addPersonPicker, addPetPicker, answerSuggest, applyFilters, applySort,
  applyTimelineFilters, backToPeople, clearFilters, clearTimelineFilters, closeModal,
  closePick, closePlaceCluster, closeSettings, editClusterName, editDate, editPersonName,
  editPlace, hidePerson, mergeAskCancel, newPlace, onAddPerson, onAddPet,
  onPeopleFilterChange, onPlaceSelect, onSemanticComposerInput, onSemanticComposerKeydown,
  onSemanticComposerPaste, onTimelineYearChange, onYearChange, openItem, openSettings,
  reassignFace, removeManualPerson, removeManualPet, renamePet, renderInfo, saveDate,
  saveNewPlace, semanticSubmit, setMapView, setStorageMetric, showSection, toPicker,
  toggleNav, togglePipelinePause, toggleStagePause, toggleTheme, undoMerge,
});
