// The pipeline status chip in the sidebar, shown on every section. It polls on
// its own rather than borrowing the Overview screen's poll, because it has to
// stay current while the user is anywhere in the app.

import {
  isCurrentSnapshot, jget, jpost, oneAtATime,
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
// The sidebar chip and the Overview health cards read the SAME pipeline
// snapshot, so they can never tell the user two different things. There is
// deliberately no table of card ids here to go with it: this file kept one for
// a while, the sixth card was added to pipeline/stages.py without it, and the
// only thing reading it -- the per-stage pause button -- silently refused to
// draw itself for a stage it had never heard of.
// Percentages arrive with one decimal; whole numbers read calmer in an
// ambient chip, and "<1%" beats a "0%" that looks stalled.
function gstatPct(pct) {
  if (pct > 0 && pct < 1) return "&lt;1%";
  return Math.min(100, Math.round(pct)) + "%";
}
function gstatRow(run) {
  const pct = run.progress && run.progress.percent != null ? run.progress.percent : null;
  // The card's own line, not a wording of the chip's own. This module used to
  // keep a table of running labels, which is how the chip came to say
  // "Indexing search…" about the card next to it reading "Semantic indexing"
  // about the feature the user had chosen as "Search by description". The
  // snapshot already carries one composed line per running stage; taking it
  // verbatim is what makes those three the same sentence.
  //
  // The trailing ellipsis meant "in progress"; the bar says that now, and
  // dropping it keeps the real text-overflow ellipsis unambiguous. A job
  // winding down after a pause says so instead of naming work it is about to
  // stop doing.
  const label = run.pausing ? "Pausing"
    : (run.message || run.label || "").replace(/…$/, "");
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
// Wrapped for the same reason the Overview's poller is, and it matters more
// here: this one runs on every section for the whole session, so on a slow
// snapshot it is the Library and the viewer that lose their connections, not
// just the health panel nobody is looking at. See `oneAtATime`.
const gstatTick = oneAtATime(async () => {
  if (!S.arch) { stopGlobalStatus(); return; }
  try {
    const snap = await jget("/api/pipeline?root=" + S.arch.id);
    if (!isCurrentSnapshot(snap, S.arch)) return;   // answered about the archive we left
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
});
export function startGlobalStatus() { stopGlobalStatus(); S.gpoll = setInterval(gstatTick, 2000); gstatTick(); }
export function stopGlobalStatus() { if (S.gpoll) { clearInterval(S.gpoll); S.gpoll = null; } }

/* Coming back to the window is the strongest sign the app gets that files were
   added: adding them means leaving Trove, dropping them in somewhere else and
   returning. The server treats it as a hint and re-checks; nothing here claims
   anything happened, and a hint that turns out to be nothing costs one walk of
   the archive folder, which the server throttles.

   This is deliberately not the only way a change is noticed -- the server also
   watches the folder, and polls regardless -- but it is the one that works on
   network shares, where filesystem events are not delivered at all. */
let notedAt = 0;
function noteReturned() {
  if (document.visibilityState !== "visible" || !S.arch) return;
  // visibilitychange and focus both fire on the way back to the window, and
  // some window managers repeat focus. One return is one hint.
  const now = Date.now();
  if (now - notedAt < 1000) return;
  notedAt = now;
  // Fire and forget, including on failure: this is an optimisation, and the
  // poll behind it is what guarantees the change is found either way.
  jpost("/api/pipeline/changed?root=" + S.arch.id).catch(() => {});
  // Ask for the new state at once rather than up to two seconds later, so the
  // card starts moving as soon as the server has something to say.
  gstatTick();
}
document.addEventListener("visibilitychange", noteReturned);
window.addEventListener("focus", noteReturned);
