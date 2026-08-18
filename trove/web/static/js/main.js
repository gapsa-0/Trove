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
  applyFilters, applySort, clearFilters, onPeopleFilterChange, onPetsFilterChange,
  onYearChange,
} from "./library.js";
import {
  onSemanticComposerInput, onSemanticComposerKeydown, onSemanticComposerPaste,
  semanticSubmit,
} from "./search.js";

import {
  MITEM, closeModal, closePick, copyText, editDate,
  editPlace, nameFace, newPlace, onAddPerson, onAddPet, openCopy, openFileLocation, onPlaceSelect,
  openItem, openRelated, reassignFace,
  removeManualPerson, removeManualPet, renderInfo, saveDate, saveNewPlace, showRelated,
  stepItem, toggleInspector, viewerBack, zoomReset, zoomStep, zoomToSlider,
} from "./item.js";
import {
  highlightFace, toggleBoxes,
} from "./boxes.js";
import {
  applyDupFilters, setDupMetric,
} from "./dups.js";
import {
  backToPets,
} from "./pets.js";
import {
  answerSuggest, backToPeople, unhidePerson,
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
  closeArchiveSetup, removeFeature, setArchiveName, submitArchiveSetup, toggleFeature,
} from "./setup.js";
import {
  closeFeatureSheet, featureSheetOpen, openFeatureSheet, saveFeatureSheet,
  toggleSheetFeature,
} from "./features.js";
import {
  closeDocs, docsHashSlug, docsOpen, docsSlug, openDocs, showDoc,
} from "./docs.js";

import {
  S,
} from "./state.js";

// Every <details> used as a popover: the Browse screen's checkbox filters and
// a person's or pet's recent-changes menu. All of them behave the way a native
// popover does -- only one stays open, and clicking elsewhere or pressing
// Escape dismisses it without changing anything. Listed once so a new popover
// joins the behaviour by naming itself here rather than by repeating these
// three listeners.
const POPOVERS = ".multi-filter[open], .histmenu[open]";
document.addEventListener("pointerdown", event => {
  document.querySelectorAll(POPOVERS).forEach(menu => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape")
    document.querySelectorAll(POPOVERS).forEach(menu => menu.removeAttribute("open"));
});
document.addEventListener("toggle", event => {
  if (event.target.matches && event.target.matches(POPOVERS))
    document.querySelectorAll(POPOVERS).forEach(menu => {
      if (menu !== event.target) menu.removeAttribute("open");
    });
}, true);

window.addEventListener("hashchange", () => {
  // A reference page can be reached by the browser's own navigation -- a link
  // between two of them, or Back out of one -- so the same routing that runs on
  // load has to run here, and before the archive match.
  const doc = docsHashSlug(location.hash);
  if (doc) { if (doc !== docsSlug()) openDocs(doc); return; }
  if (docsOpen()) closeDocs();
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

/* Clicking into an embedded PDF hands the keyboard to the browser's own viewer,
   which pages with the arrows and swallows them before the listener below ever
   runs. Nothing here can change that: the keys are delivered to a document
   inside an iframe that is the browser's, not ours, and nothing in it bubbles
   out. The viewer's own arrow buttons are unaffected, and clicking anywhere
   back on the page gives the keys back, so the way out is the way anyone would
   already take.

   This used to be announced -- a pill over the stage saying the arrows belonged
   to the document. The announcement cost more than the confusion it replaced:
   it was the only mode the app had, its advertised way out (Esc) could not
   reach the page that promised it, and it left the button labelled "Close" not
   closing. Explaining a browser behaviour is not worth a mode. */

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("settings-drawer").classList.contains("open")) {
    closeSettings(); return;
  }
  // Before the reference pages and the viewer: the sheet sits over the archive,
  // so it is the topmost thing Escape can mean while it is open.
  if (e.key === "Escape" && featureSheetOpen()) { closeFeatureSheet(); return; }
  if (e.key === "Escape" && docsOpen()) { closeDocs(); return; }
  if (e.key === "Escape") { closeModal(); document.getElementById("viewer").focus(); return; }
  if (!MITEM || !document.getElementById("modal").classList.contains("open")) return;
  // Not while a field is being typed into: the date editor and the place
  // name live inside this panel.
  const el = document.activeElement, tag = el ? el.tagName : "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (e.key === "ArrowLeft") { e.preventDefault(); stepItem(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); stepItem(1); }
  else if (e.key === "i" || e.key === "I") { e.preventDefault(); toggleInspector(); }
  else if (e.key === "b" || e.key === "B") { e.preventDefault(); toggleBoxes(); }
  // The keys every image viewer has. "=" is the unshifted "+" on most layouts.
  else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomStep(1); }
  else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomStep(-1); }
  else if (e.key === "0") { e.preventDefault(); zoomReset(); }
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
  addArchiveFromForm, answerSuggest, applyFilters,
  applyDupFilters, applySort, applyTimelineFilters, backToPeople, backToPets,
  clearFilters, clearTimelineFilters,
  closeArchiveSetup, closeDocs, closeFeatureSheet, closeModal, closePick, closePlaceCluster,
  closeSettings,
  copyText,
  editClusterName, editDate, editPlace,
  highlightFace,
  mergeAskCancel,
  nameFace, newPlace, onAddPerson,
  onAddPet, onPeopleFilterChange, onPetsFilterChange, onPlaceSelect, onSemanticComposerInput,
  onSemanticComposerKeydown, onSemanticComposerPaste, onTimelineYearChange, onYearChange,
  openCopy, openDocs, openFeatureSheet, openFileLocation, openItem, openRelated,
  openSettings,
  reassignFace,
  removeFeature, removeManualPerson,
  removeManualPet,
  renderInfo, saveDate, saveFeatureSheet, saveNewPlace, semanticSubmit, setArchiveName,
  setDupMetric, setMapView,
  setStorageMetric, showDoc, showRelated, showSection, stepItem, submitArchiveSetup, toPicker,
  toggleBoxes, toggleFeature, toggleInspector, toggleNav, toggleSheetFeature,
  viewerBack,
  togglePipelinePause,
  toggleStagePause, toggleTheme, undoMerge, unhidePerson, zoomReset, zoomStep, zoomToSlider,
});
