// The pipeline status chip in the sidebar, shown on every section. It polls on
// its own rather than borrowing the Overview screen's poll, because it has to
// stay current while the user is anywhere in the app.

import {
  jget,
} from "./api.js";
import {
  loadGrid, resetGridResults,
} from "./library.js";
import {
  S,
} from "./state.js";

/* ---------- persistent pipeline status (sidebar, shown on every section) ----
   The pipeline runs itself; this ambient chip is the only status the user
   needs and it carries no controls. */
const JOB_LABEL = {
  scan: "Scanning files…", enrich: "Reading metadata…", detect: "Detecting people & pets…",
  face_cluster: "Updating people…", places: "Updating map places…", dedup: "Finding duplicates…", semantic: "Indexing search…"
};
// The sidebar chip and the Overview health cards read the SAME pipeline
// snapshot, so they can never tell the user two different things.
export const CARD_KIND = { scan: "scan", dedup: "dedup", detect: "detect", places: "places", semantic: "semantic" };
// Percentages arrive with one decimal; whole numbers read calmer in an
// ambient chip, and "<1%" beats a "0%" that looks stalled.
function gstatPct(pct) {
  if (pct > 0 && pct < 1) return "&lt;1%";
  return Math.min(100, Math.round(pct)) + "%";
}
function gstatRow(run) {
  const pct = run.progress && run.progress.percent != null ? run.progress.percent : null;
  // The label's trailing ellipsis meant "in progress"; the bar says that now,
  // and dropping it keeps the real text-overflow ellipsis unambiguous.
  const label = (JOB_LABEL[CARD_KIND[run.id]] || run.label).replace(/…$/, "");
  return `<div class="grow"><div class="gline"><span class="gtxt">${label}</span>`
    + (pct != null ? `<span class="gpct">${gstatPct(pct)}</span>` : "") + `</div>`
    + (pct != null
      ? `<div class="gbar"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`
      : `<div class="gbar ind"><i></i></div>`)
    + `</div>`;
}
export function renderGstat(snap) {
  const el = document.getElementById("gstat"); if (!el) return;
  // Stages plus the non-stage jobs a user action kicks (face_cluster /
  // pet_cluster). Those hold the writer lock too, so leaving them out made
  // the app look stalled for no visible reason.
  const runs = snap && snap.stages
    ? snap.stages.filter(s => s.state === "running").concat(snap.extra || [])
    : [];
  // Several PARALLEL_KINDS stages can run at once; one row each. The
  // collapsed rail falls back to .gmini (see the .gstat CSS).
  const mini = `<span class="gmini"><span class="spin"></span>`
    + (runs.length > 1 ? `<span class="gcount">×${runs.length}</span>` : "") + `</span>`;
  if (runs.length) {
    el.title = runs.map(r => `${r.label}: ${r.message || ""}`).join("\n");
    el.innerHTML = mini + runs.map(gstatRow).join("");
  } else if (!snap) {
    el.title = "Checking for new work…";
    el.innerHTML = `<div class="gstate"><span class="dot pending"></span><span class="gtxt">Checking for new work…</span></div>`;
  } else if (snap.overall === "idle") {
    el.title = "Up to date";
    el.innerHTML = `<div class="gstate"><span class="dot ok"></span><span class="gtxt">Up to date</span></div>`;
  } else if (snap.overall === "paused") {
    // Nothing is actually running (the `runs` branch above already
    // caught that); reads as stopped, not "Working…".
    el.title = "Background processing is paused";
    el.innerHTML = `<div class="gstate"><span class="dot check"></span><span class="gtxt">Paused</span></div>`;
  } else {
    // Work is waiting to run (queued/blocked) but nothing is on the writer yet.
    el.title = "Work is queued";
    el.innerHTML = `<div class="gstate"><span class="dot pending"></span><span class="gtxt">Working…</span></div>`;
  }
}
async function gstatTick() {
  if (!S.arch) { stopGlobalStatus(); return; }
  try {
    const snap = await jget("/api/pipeline?root=" + S.arch.id);
    S.pipeline = snap;
    renderGstat(snap);
    // A library opened just before the scanner commits its first batch used to
    // remain an empty wall until the user manually changed a filter or route.
    // Refresh only that empty state, so active browsing is never interrupted.
    const scanning = (snap.stages || []).some(s => s.id === "scan" && s.state === "running"), g = S.grid;
    if (scanning && S.section === "library" && g && g.loaded === 0 && !g.refreshing) {
      g.refreshing = true;
      setTimeout(() => {
        if (S.section === "library" && S.grid === g && g.loaded === 0) {
          resetGridResults(g);
          loadGrid().finally(() => { if (S.grid === g) g.refreshing = false; });
        } else if (S.grid === g) {
          g.refreshing = false;
        }
      }, 1500);
    }
  } catch {
    // A poll tick that fails is a non-event: the next one is two seconds away
    // and the chip simply keeps its last value. Reporting it would fill the
    // console every time the server restarts under the user.
  }
}
export function startGlobalStatus() { stopGlobalStatus(); S.gpoll = setInterval(gstatTick, 2000); gstatTick(); }
export function stopGlobalStatus() { if (S.gpoll) { clearInterval(S.gpoll); S.gpoll = null; } }
