// The Overview screen: the headline stats, the storage-by-type panel, and the
// health cards that report what the background pipeline is doing. The poll
// itself lives in pipeline.js and serves every screen; this one subscribes for
// the detail only the Overview draws.

import {
  renderGstat,
} from "./status.js";
import {
  onSnapshot, refreshPipelineNow,
} from "./pipeline.js";
import {
  jget, jpost,
} from "./api.js";
import {
  docsButton,
} from "./docs.js";
import {
  esc, fmtBytes, fmtElapsed, toast,
} from "./dom.js";
import {
  ICONS, S, TYPE_COL, archiveSections, typeLabel,
} from "./state.js";

// A number we do not have yet. Not "0" — that is a different claim, and this
// screen's whole job is to say what the archive actually contains. The dash
// sits in the same tabular-nums slot the real figure will take, so nothing
// moves when it arrives.
const UNKNOWN = `<span class="stat-unknown">-</span>`;
const statValue = v => (v == null ? UNKNOWN : v.toLocaleString());

// One headline stat, and the screen it opens. Every tile in this row is a
// doorway, so a tile whose screen this archive does not run is not rendered at
// all — leaving it would be a button that silently does nothing, which is the
// exact failure the app goes to some length elsewhere to prevent.
//
// The label says where the door goes, because nothing else did: the tile's own
// text is a name and a number, so a screen reader announced "Redundant copies
// 92, button" and pressing it moved the user to a screen they were not told
// about.
const SECTION_NAME = {
  library: "Browse", timeline: "Timeline", places: "Places", dups: "Duplicates",
};
function statTile(section, colour, label, id, value) {
  if (!archiveSections(S.arch).some(s => s.id === section)) return "";
  return `<button class="stat" onclick="showSection('${section}')"
      aria-label="${label}: ${statValue(value).replace(/<[^>]*>/g, "")}. Open ${SECTION_NAME[section] || section}">
      <span class="metric-icon ${colour}">${ICONS[section]}</span>
      <div><div class="k">${label}</div>
      <div class="v" id="${id}">${statValue(value)}</div></div>
      <span class="stat-go" aria-hidden="true">›</span></button>`;
}

/* Mark the tiles whose figure is behind a stopped stage.

   Each of these numbers is produced by a stage that can be paused, and the row
   presented all of them as equally current facts about the archive: with
   Duplicates paused, "Redundant copies" read 92 while the truth was 112, in the
   same type as the two live figures beside it and with nothing to say so.

   The tile is dimmed and given the reason on hover rather than being hidden or
   annotated in place: the number is still the last true answer, and the row has
   no room for a sentence per tile. */
const STAT_STAGE = { "ov-total": "scan", "ov-enriched": "scan", "ov-gps": "places", "ov-dups": "dedup" };
function markStaleStats(snap) {
  const stages = (snap && snap.stages) || [];
  for (const [id, stageId] of Object.entries(STAT_STAGE)) {
    const tile = document.getElementById(id)?.closest(".stat");
    if (!tile) continue;
    const stage = stages.find(s => s.id === stageId);
    // A folder that cannot be read makes every one of these a fact about the
    // catalogue rather than about the archive, so all four are dimmed together
    // -- which is what makes the notice above felt rather than merely stated.
    const why = !snap ? ""
      : snap.root_missing ? "The folder cannot be read, so this is what was catalogued last time."
        : snap.paused ? "Background processing is paused, so this figure may be out of date."
          : stage && (stage.paused || stage.stalled)
            ? `${stage.label} is paused, so this figure may be out of date.` : "";
    tile.classList.toggle("stat-stale", !!why);
    if (why) tile.title = why; else tile.removeAttribute("title");
  }
}

// What the picker already handed us, shaped as a partial /api/summary.
//
// The card the user just clicked showed these two figures, and /api/summary
// answers them with the same two aggregates over the same rows — COUNT(*) and
// SUM(size) under _VISIBLE, the root clause being a no-op given the one root
// per archive database that db.reconcile_root enforces. So this is not an
// estimate that the real answer might contradict a moment later: it is the same
// number, arriving sooner. Everything else is null, and draws as unknown.
function seededSummary() {
  return {
    total: S.arch ? S.arch.files : null,
    size: S.arch ? S.arch.size : null,
    types: null,
    enriched: null,
    with_gps: null,
  };
}

export async function renderOverview(m) {
  const gen = S.nav, root = S.arch.id;
  // Asked for before the first paint and awaited after it: the six requests are
  // in flight while the page draws, rather than the page waiting on all six.
  const pending = Promise.all([
    jget("/api/summary?root=" + root),
    jget("/api/dups/summary?root=" + root),
    jget("/api/faces/summary?root=" + root),
    jget("/api/pets/summary?root=" + root).catch(() => null),
    jget("/api/browse/semantic/status?root=" + root).catch(() => null),
    jget("/api/browse/text/status?root=" + root).catch(() => null)]);
  paintOverview(m, seededSummary());
  const [s, ds, fs, ps, ss, ts] = await pending;
  if (gen !== S.nav) return;   // user switched sections while these were loading
  S.dupsum = ds;
  S.facesum = fs;
  S.petsum = ps;
  S.semanticsum = ss;
  S.textsum = ts;
  S.summary = s;
  // The shell is already on screen and its ids are stable, so the numbers are
  // filled in where they stand rather than by rebuilding the page under the
  // user — the same way refreshPipeline keeps them climbing while work runs.
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.innerHTML = statValue(v); };
  set("ov-total", s.total);
  set("ov-enriched", s.enriched);
  set("ov-gps", s.with_gps);
  set("ov-dups", ds ? ds.duplicates : null);
  renderStoragePanel(s);
  renderHealthCards();   // its "done" lines quote the summaries that just landed
}

function paintOverview(m, s) {
  m.innerHTML = `<div class="pagehead">
      <div><h2 class="sec">Library overview</h2>
      <p>Everything important about this archive, at a glance.</p></div>
      ${docsButton("overview")}
    </div>
    <div class="statrow">
      ${statTile("library", "blue", "All files", "ov-total", s.total)}
      ${statTile("timeline", "violet", "With a date", "ov-enriched", s.enriched)}
      ${statTile("places", "green", "With a location", "ov-gps", s.with_gps)}
      ${statTile("dups", "orange", "Redundant copies", "ov-dups", s.duplicates)}
    </div>
    <div class="overview-grid">
      <div class="panel status-panel"><div class="panel-heading"><span class="panel-symbol">${ICONS.overview}</span><div><h3>Library health</h3><p>Progress on everything this archive runs</p></div><button type="button" class="btn sec pause-btn" id="pause-btn" onclick="togglePipelinePause()">Pause all</button></div>
        <div id="syncstatus"></div><div id="jobarea"></div>
        <div class="panel-foot">
          <button type="button" class="quietbtn manage-features" onclick="openFeatureSheet()">Manage features</button>
        </div>
      </div>
      <div class="panel type-panel"><div class="panel-heading"><div><h3>Storage</h3><p id="ov-storage-caption">${storageCaption(s)}</p></div><div class="metric-switch" id="storage-switch"></div></div>
        <div class="storage-bar" id="typebar"></div><table class="types" id="typetbl"></table>
      </div>
    </div>`;
  S.summary = s;
  renderStoragePanel(s);   // reads S.summary back when the metric switch flips
  renderHealthCards();   // paint immediately from the last snapshot (if any)
  // Nothing to start: the one poller (pipeline.js) has been running since the
  // archive was opened, and this screen's subscriber is registered at module
  // level. This used to start a second poller of its own on top of the chip's.
}
// The archive's own totals, or what to say before they are known. Both figures
// come from the same payload, so there is no half-known case to word.
const storageCaption = s => (s.size == null || s.total == null
  ? "Reading the catalogue…"
  : `${fmtBytes(s.size)} across ${s.total.toLocaleString()} items`);
// Fills the "Storage" panel (byte total + one bar + table) from an
// /api/summary payload. Called from renderOverview on first paint AND from
// refreshPipeline on every poll tick while the pipeline is busy, so bytes
// reclaimed/added by scan+dedup show up live instead of only at the
// busy→idle edge (the caption, switch, bar and table share stable ids).
//
// Files and size are two readings of the same breakdown and they disagree
// sharply -- in a photo archive the videos are a rounding error by count and
// most of the disk. Showing both at once meant two bars and a legend of
// percentages nobody asked for. One bar answers whichever question the
// switch is set to; the exact numbers live in the table, where numbers
// belong, and the per-type detail appears on hover.
const STORAGE_METRICS = {
  size: {
    label: "Size", of: "of size",
    value: t => t.size || 0,
    text: t => fmtBytes(t.size || 0),
  },
  files: {
    label: "Files", of: "of files",
    value: t => t.count,
    text: t => `${t.count.toLocaleString()} file${t.count === 1 ? "" : "s"}`,
  },
};
function storageMetric() {
  return STORAGE_METRICS[S.storageMetric] ? S.storageMetric : "size";
}
export function setStorageMetric(metric) {
  S.storageMetric = metric;
  if (S.summary) renderStoragePanel(S.summary);
}
/* An archive with nothing in it yet.

   Which of the two reasons it is depends on whether the scan has finished
   looking: until it has, "nothing here" is a statement about how far it has
   got; once it has, it is a statement about the folder. Both are worth telling
   apart, because only one of them is something the user should act on. */
function renderEmptyStorage() {
  const scan = ((S.pipeline && S.pipeline.stages) || []).find(x => x.id === "scan");
  const looked = !!scan && scan.state === "up_to_date";
  const missing = !!(S.pipeline && S.pipeline.root_missing);
  const line = missing
    ? "The folder cannot be reached, so there is nothing to measure."
    : looked
      ? "Trove has been through this folder and found no photos, videos, audio or documents in it."
      : "Nothing catalogued yet. This fills in as the folder is read.";
  const bar = document.getElementById("typebar");
  const tbl = document.getElementById("typetbl");
  if (bar) bar.innerHTML = `<p class="storage-empty">${line}</p>`;
  if (tbl) tbl.innerHTML = "";
  const sw = document.getElementById("storage-switch");
  if (sw) sw.innerHTML = "";     // nothing to switch between
}
function renderStoragePanel(s) {
  const cap = document.getElementById("ov-storage-caption");
  if (cap) cap.textContent = storageCaption(s);
  // The caption above is answerable from the picker's payload; the breakdown
  // below is not, and an empty bar is the honest thing to show until it lands.
  // Drawing one segment for "everything" would be inventing a shape.
  if (!s.types) return;
  // Nothing catalogued: a bar with no segments and a table with a header and no
  // rows is a skeleton, and an empty grey bar is indistinguishable from one
  // still loading. Say the one thing there is to say instead. An empty archive
  // is a real and recoverable situation -- the wrong folder was added, or the
  // folder holds nothing Trove reads -- so it gets a sentence rather than a
  // chart of nothing.
  if (!s.total) { renderEmptyStorage(); return; }
  const key = storageMetric(), metric = STORAGE_METRICS[key];
  const colour = t => TYPE_COL[t.type] || TYPE_COL.other;
  // Sorted by the metric on screen, so the bar and the table below it read
  // in the same order whichever way the switch is set.
  const types = s.types.filter(t => t.count > 0)
    .sort((a, b) => metric.value(b) - metric.value(a));
  const total = types.reduce((sum, t) => sum + metric.value(t), 0) || 1;
  // A type can be a real part of the archive and still round to 0.0%
  // (30 files, 98.7 GB): "<0.1%" is honest where "0.0%" reads as nothing.
  const share = t => {
    const p = 100 * metric.value(t) / total;
    return p > 0 && p < 0.1 ? "<0.1%" : `${p.toFixed(1)}%`;
  };

  const sw = document.getElementById("storage-switch");
  // Either side switches. It reads as one switch with two ends, so pressing
  // the lit end has to do something -- and there is only one thing it could
  // mean. The active button therefore carries the *other* metric's id; every
  // metric change re-renders this, so the handlers stay correct.
  const other = Object.keys(STORAGE_METRICS).find(id => id !== key) || key;
  if (sw) sw.innerHTML = Object.entries(STORAGE_METRICS).map(([id, m]) =>
    `<button type="button" class="${id === key ? "on" : ""}"
        onclick="setStorageMetric('${id === key ? other : id}')">${m.label}</button>`).join("");

  const typebar = document.getElementById("typebar");
  if (typebar) {
    typebar.innerHTML = `<div class="type-summary-bar">${types.map((t, i) =>
      `<div class="type-summary-segment" data-i="${i}" style="width:${100 * metric.value(t) / total}%;background:${colour(t)}"></div>`
    ).join("")}</div><div class="storage-tip" hidden></div>`;
    const tip = typebar.querySelector(".storage-tip");
    typebar.querySelectorAll(".type-summary-segment").forEach(seg => {
      seg.onmouseenter = () => {
        const t = types[+seg.dataset.i];
        tip.innerHTML = `<span class="swatch" style="background:${colour(t)}"></span>${typeLabel(t.type)} <span class="muted">· ${metric.text(t)} · ${share(t)} ${metric.of}</span>`;
        // Centred on the segment, then held inside the panel so a sliver at
        // either end doesn't push the tooltip off the edge.
        const mid = seg.offsetLeft + seg.offsetWidth / 2;
        tip.hidden = false;
        const half = tip.offsetWidth / 2;
        tip.style.left =
          Math.max(half, Math.min(typebar.clientWidth - half, mid)) + "px";
      };
      seg.onmouseleave = () => { tip.hidden = true; };
    });
  }

  const tbl = document.getElementById("typetbl");
  if (!tbl) return;
  // The header follows the switch. "Share" alone changed what it divided by
  // while sitting in the column next to Size, so with Files selected a row read
  // "image · 304 · 17.9 MB · 93.3%" and the percentage attached itself to the
  // number beside it. 93.3% is its share of files; its share of size is 97%.
  // The hover tip on the bar has always said "of files" / "of size"; this is
  // the same qualifier, where someone reading the table actually is.
  tbl.innerHTML = `<tr><th>Type</th><th>Files</th><th>Size</th><th>% ${metric.of}</th></tr>`;
  types.forEach(t => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="swatch" style="background:${colour(t)}"></span>${typeLabel(t.type)}</td>
      <td class="num">${t.count.toLocaleString()}</td>
      <td class="num">${fmtBytes(t.size)}</td>
      <td class="num share">${share(t)}</td>`;
    tbl.appendChild(tr);
  });
}
// Everything runs automatically in the background; this panel only reports
// what's happening. Every card is rendered the SAME way from one resolved
// state the server computes (running/queued/checking/blocked/up_to_date/
// unavailable/error), no per-card logic here, so the five cards can never
// disagree with each other or with what the pipeline is actually doing.
//
// `checking` draws like `queued` on purpose: both mean "not done, nothing to
// show yet". It is the scan card while the folder is being counted, which on a
// large archive is the first twenty seconds after it is opened.
const HEALTH_CARDCLASS = { running: "running", queued: "pending", checking: "pending", blocked: "", up_to_date: "", unavailable: "", error: "error" };
const HEALTH_DOT = { queued: "pending", checking: "pending", blocked: "check", up_to_date: "ok", unavailable: "check", error: "check" };
function healthDoneMessage(id) {
  // The "done" (up_to_date) line reuses the per-domain summary numbers the
  // Overview already holds, so it reads as a result, not a bare "done".
  const s = S.summary, ds = S.dupsum, fs = S.facesum, ps = S.petsum, ss = S.semanticsum;
  const ts = S.textsum;
  switch (id) {
    case "scan": return `${(s && s.total || 0).toLocaleString()} files catalogued`;
    case "dedup": return ds && ds.duplicates
      ? `${ds.duplicates.toLocaleString()} redundant cop${ds.duplicates === 1 ? "y" : "ies"} found`
      : "No redundant copies found";
    case "detect": {
      const parts = [];
      if (fs && fs.faces) parts.push(`${fs.faces.toLocaleString()} face${fs.faces === 1 ? "" : "s"}`);
      if (ps && ps.detections) parts.push(`${ps.detections.toLocaleString()} animal${ps.detections === 1 ? "" : "s"}`);
      return parts.length ? parts.join(" · ")
        : `${(fs && fs.scanned || 0).toLocaleString()} photo${fs && fs.scanned === 1 ? "" : "s"} analyzed`;
    }
    case "places": return s && s.with_gps
      ? `${s.with_gps.toLocaleString()} location${s.with_gps === 1 ? "" : "s"} mapped`
      : "No locations found";
    case "semantic": return ss && ss.indexed
      ? `${ss.indexed.toLocaleString()} item${ss.indexed === 1 ? "" : "s"} indexed`
      : (ss && ss.configured ? "Ready to index" : "Not configured");
    // Both halves of the text card fill one index from one pass, so there is
    // no per-half count to quote and this figure is the honest answer whichever
    // of them is on: what the archive can now search by what it says.
    case "text": return ts && ts.read
      ? `${ts.read.toLocaleString()} file${ts.read === 1 ? "" : "s"} read`
      : "Nothing read yet";
    default: return "";
  }
}
/* What a stopped card says, which used to be the bare word "Paused".

   Two things were lost by that. A stage stopped with work behind it knows how
   much -- the server sends `pending` on every tick -- and that is the number
   the person who paused it comes back for; "Paused" alone is the same sentence
   after an hour and after a month. And a stage stopped with nothing waiting had
   its done message printed instead, so a card that was dimmed and marked with a
   stopped dot simultaneously read "112 redundant copies found", which is the
   state it is *not* in any more.

   `counted` is what says the figure means something: dedup rebuilds wholesale
   and reports no backlog, so it has nothing to add and says so by omission. */
function pausedMessage(stage) {
  if (stage.counted && stage.pending) {
    return `Paused · ${stage.pending.toLocaleString()} waiting`;
  }
  const done = stage.state === "up_to_date" ? healthDoneMessage(stage.id) : "";
  return done ? `Paused · ${done}` : "Paused";
}
function healthCard(stage) {
  const st = stage.state;
  const allPaused = !!(S.pipeline && S.pipeline.paused);
  // Pausing only ASKS the running job to stop, at its next batch checkpoint,
  // so there are seconds where the stage is paused and still working. Saying
  // "Scanning files…" through them reads as a button that did nothing, then as
  // a stage that stopped for no reason. The server says the same thing on its
  // next poll (stages.snapshot sets `pausing`); repeating the rule here is
  // what makes the click feel instant, up to 1.2s before that poll lands.
  const pausing = st === "running" && (allPaused || !!stage.paused || !!stage.pausing);
  // Paused cards should read as stopped, not "about to run": swap the amber
  // pending dot for the same neutral one blocked cards use. `stalled` covers
  // both a stage the user paused and one queued behind a paused stage --
  // neither is going to start. A still-running (winding-down) stage is
  // untouched here -- its dot/spinner already come from `st`.
  const stopped = (allPaused || stage.stalled) && st !== "running";
  const dot = stage.next ? "next" : (stopped ? "check" : (HEALTH_DOT[st] || "check"));
  // The head is identity and the line below it is state, which is what makes
  // room for the mark: the feature's own icon, the one it carried on the card
  // the user pressed to switch this work on. The spinner/dot moved down to sit
  // with the words it qualifies rather than competing with the mark up here.
  const head = `<span class="feat-mark">${ICONS[stage.icon] || ""}</span>`
    + stage.label + stagePauseButton(stage);
  const mark = st === "running" ? `<span class="spin"></span>` : `<span class="dot ${dot}"></span>`;
  const message = pausing ? "Pausing…"
    : stopped ? pausedMessage(stage)
      : (st === "up_to_date" ? healthDoneMessage(stage.id) : (stage.message || ""));
  let detail = "", bar = "";
  // Not gated on "running": a stage stopped mid-run keeps the bar it reached
  // (the server hands it back for a paused card -- see stages._stopped_progress),
  // because the run is suspended rather than discarded. A stage that is still
  // preparing has no bar to show, and the server sends none.
  //
  // The counts follow the message on its own line rather than sitting under a
  // bar of their own: a full-width row has the room, and it leaves the bar as
  // the one thing in the row that moves.
  // Each part earns its place once. The percentage is the bar directly beneath
  // this line, so printing it as well said the same thing twice; what the bar
  // cannot give is the absolute size of the job and how long it has been at it,
  // which is what stays. Without a total there is no percentage to know -- the
  // bar sits empty and the text used to claim "0%", which is not "unknown".
  const p = stage.progress;
  if (p && (p.total || p.done)) {
    const pct = Math.max(0, Math.min(100, p.percent != null ? p.percent : 0));
    const counts = (p.done || 0).toLocaleString()
      + (p.total ? " / " + p.total.toLocaleString() : "");
    const parts = [counts, fmtElapsed(p.elapsed)];
    if (p.current) parts.push(p.current);
    detail = `<span class="health-task-count">${parts.join(" · ")}</span>`;
    bar = `<div class="health-task-bar"><i style="width:${pct}%"></i></div>`;
  }
  // Beyond pause/resume no card offers a call to action. Semantic indexing
  // was the only one that did ("Add API key"), and it has nothing left to
  // configure: it is unavailable only when the app was installed without its
  // optional dependencies, which no button here could fix.
  const cls = stopped ? "paused" : (stage.next ? "next" : (HEALTH_CARDCLASS[st] || ""));
  // The rail node says where this row sits in the chain: filled for the trunk
  // every archive runs, hollow for what clips onto it. The line itself is
  // drawn by CSS across the whole column, so the fork reads as one shape
  // rather than five decorated rows.
  return `<div class="health-task ${cls}${stage.always_runs ? " trunk" : ""}">
      <span class="health-rail" aria-hidden="true"><i class="health-node"></i></span>
      <div class="health-task-body">
        <div class="health-task-head">${head}</div>
        <div class="health-task-state">${mark}${message}${detail}</div>${bar}</div></div>`;
}
// Per-stage pause/resume. One button per card, on top of
// the whole-pipeline switch in the panel heading: it stops just this stage
// and lets the others carry on. Deliberately inert while the whole pipeline
// is paused -- nothing is running, so there is nothing here to stop.
const PAUSE_GLYPH = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="5" width="4" height="14" rx="1.2" fill="currentColor"/><rect x="13" y="5" width="4" height="14" rx="1.2" fill="currentColor"/></svg>';
const PLAY_GLYPH = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5Z" fill="currentColor"/></svg>';
function stagePauseButton(stage) {
  // An unavailable stage has no work to pause: its backend isn't installed, so
  // there is nothing running to stop. That is the only card without a button.
  //
  // There used to be a second condition, a hardcoded list of the five card ids
  // that were stages -- meant to keep the button off the non-stage jobs in
  // `extra`, which are user-kicked one-offs. Those never reach this function
  // (the sidebar chip draws them; this panel only ever maps `snap.stages`), and
  // the list was never told about the sixth card, so the text stage quietly had
  // no way to be paused on its own. What decides this is the state, not a list
  // of names that has to be maintained beside the one in pipeline/stages.py.
  if (stage.state === "unavailable") return "";
  const globallyPaused = !!(S.pipeline && S.pipeline.paused);
  const off = !!stage.paused;
  const label = off ? `Resume ${stage.label}` : `Pause ${stage.label}`;
  const title = globallyPaused
    ? "All background processing is paused" : label;
  return `<button type="button" class="health-task-btn" title="${title}"
      aria-label="${label}" ${globallyPaused || S.pausing ? "disabled" : ""}
      onclick="toggleStagePause('${stage.id}',event)">${off ? PLAY_GLYPH : PAUSE_GLYPH}</button>`;
}
export async function toggleStagePause(id, event) {
  if (event) event.stopPropagation();
  if (!S.arch || S.pausing) return;
  const stage = (S.pipeline && S.pipeline.stages || []).find(s => s.id === id);
  if (!stage) return;
  const next = !stage.paused;
  // Paint the new state at once (the poll is up to 1.2s away), then let
  // refreshPipeline replace it with whatever the server actually did. A card
  // still running turns into "Pausing…" from this alone -- see healthCard.
  stage.paused = next;
  stage.pausing = next && stage.state === "running";
  S.pausing = true;
  renderHealthCards();
  renderGstat(S.pipeline);   // the sidebar chip reads `pausing` off the same object
  try {
    const r = await jpost("/api/pipeline/pause", { paused: next, stage: id });
    if (r && r.error) toast(r.error, true);
  } catch {
    toast(next ? "Could not pause this step." : "Could not resume this step.", true);
  } finally { S.pausing = false; }
  await refreshPipelineNow();
}
/* The archive's folder is not there.

   Leads the panel, because every figure below it is about a catalogue rather
   than about files that can be opened -- the thumbnails and the originals all
   answer 404 in this state. The scheduler has always known: `_scan_backlog`
   asks `Path.is_dir()` and reports a backlog of 0, on the correct reasoning
   that a folder which is gone is owed no scan. But 0 is also what a fully
   indexed archive looks like, so this screen showed green dots and "up to
   date" over an archive nothing in the app could read.

   Says what to do about it, and does not offer to fix it: nothing in Trove can
   mount a drive, and a button that only apologises is worse than a sentence. */
function missingRootNotice(path) {
  return `<div class="health-missing" role="status">
      <b>This folder cannot be found.</b>
      <p>Trove is showing what it catalogued last time. The files themselves cannot
      be read, so thumbnails and originals will not open, and nothing new will be
      indexed until the folder is back.</p>
      <p class="health-missing-path">${esc(path || "")}</p>
      <p>If it is on a drive or a network share, connect it — Trove picks up again
      on its own. If the folder was moved or renamed, remove this archive from the
      start page and add it at its new location; your files are not touched either
      way.</p>
    </div>`;
}
function renderHealthCards() {
  const el = document.getElementById("syncstatus"); if (!el) return;
  const snap = S.pipeline;
  renderPauseControl();
  if (!snap || !snap.stages) {
    el.innerHTML = `<div class="health-grid"><div class="health-task running"><div class="health-task-body"><div class="health-task-state"><span class="spin"></span>Checking for work…</div></div></div></div>`;
    return;
  }
  el.innerHTML = (snap.root_missing ? missingRootNotice(S.arch && S.arch.path) : "")
    + `<div class="health-grid">${snap.stages.map(healthCard).join("")}</div>`;
  markStaleStats(snap);
}
// Whole-pipeline pause/resume: pausing cancels every
// running job at its next batch checkpoint (see JobManager.set_paused), and
// the scheduler simply stops starting new ones until resumed. Individual
// stages have their own buttons on the cards (stagePauseButton); this one
// outranks them, which is why it disables them while it is on.
//
// Three states, not two. Until the first snapshot lands there is no answer to
// report: `paused` is per-archive and persisted (config/archives.py), so an
// archive that was paused when it was last closed opens paused, and a button
// that read the missing snapshot as `false` announced "Pause all" over a
// pipeline that was already stopped -- and, pressed, posted `paused: true` to
// stop it again, which is why the "Checking for work…" window has to be a
// state of its own rather than a shrug that lands on "not paused".
// There is no banner under this any more. A line reading "Paused — background
// processing is stopped" said a third time what the button already says and
// what every card below it already says ("Paused", on each one, with the dot to
// match), and it moved the chain down a row to do it.
function renderPauseControl() {
  const btn = document.getElementById("pause-btn");
  if (!btn) return;
  const known = !!S.pipeline;
  const paused = !!(S.pipeline && S.pipeline.paused);
  btn.textContent = known ? (paused ? "Resume all" : "Pause all") : "Checking…";
  btn.disabled = !known || !!S.pausing;
}
export async function togglePipelinePause() {
  // Not until the snapshot says what is being toggled. The button is disabled
  // through that window, so this is the belt to its braces: without it, a
  // keyboard activation that beat the first poll would compute "the opposite of
  // false" and pause a pipeline that was already paused.
  if (!S.arch || S.pausing || !S.pipeline) return;
  const next = !S.pipeline.paused;
  S.pausing = true;
  // Same instant repaint as the per-stage button: every still-running card
  // turns into "Pausing…" now rather than at the next poll.
  S.pipeline.paused = next;
  for (const s of S.pipeline.stages || []) s.pausing = next && s.state === "running";
  for (const e of S.pipeline.extra || []) e.pausing = next;
  renderHealthCards();
  renderGstat(S.pipeline);
  try {
    const r = await jpost("/api/pipeline/pause", { paused: next });
    if (r && r.error) toast(r.error, true);
  } catch { toast(next ? "Could not pause processing." : "Could not resume processing.", true); }
  finally { S.pausing = false; }
  await refreshPipelineNow();
}
// What the Overview does with a snapshot: redraw its cards, and keep the top
// stat numbers climbing while work is running.
//
// Subscribed at module level, not started and stopped with the screen. The
// fetch belongs to pipeline.js and runs for the whole session anyway, so there
// is nothing to save by unsubscribing -- and `renderHealthCards` already
// returns when its panel is not on screen, which is the check a per-section
// subscription would have amounted to.
onSnapshot(async snap => {
  renderHealthCards();
  // What an empty archive's storage line says turns on whether the scan has
  // finished looking -- "nothing yet" while it is still reading the folder,
  // "there is nothing here" once it has. That is a fact from the snapshot, not
  // from the summary, so it has to be redrawn when the snapshot moves and not
  // only when the numbers do.
  if (S.section === "overview" && S.summary && !S.summary.total) renderStoragePanel(S.summary);
  const area = document.getElementById("jobarea"); if (area) area.innerHTML = "";
  // Paused counts as not busy: nothing can change, so don't keep re-running
  // the summary/duplicate queries behind the user's back. Neither does
  // "checking" -- the disk walk has not answered yet, so no number it feeds
  // has moved, and asking every tick for the length of a 20s walk is exactly
  // the load this whole rework exists to remove.
  const busy = !["idle", "checking"].includes(snap.overall) && !snap.paused;
  if (busy && S.section === "overview") {
    // Numbers should climb live while work runs, without waiting for the
    // whole pipeline to finish.
    try {
      const [s, ds] = await Promise.all([
        jget("/api/summary?root=" + S.arch.id),
        jget("/api/dups/summary?root=" + S.arch.id)]);
      S.summary = s; S.dupsum = ds;
      const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
      set("ov-total", s.total.toLocaleString());
      set("ov-enriched", s.enriched.toLocaleString());
      set("ov-gps", s.with_gps.toLocaleString());
      set("ov-dups", ds.duplicates.toLocaleString());
      renderStoragePanel(s);
    } catch { /* non-critical; next tick retries */ }
  }
  // On the busy→idle edge, re-render the Overview once so the "done" messages
  // pick up the final summary numbers. Guarding on the transition avoids an
  // endless render→subscribe→render loop.
  const wasBusy = S.pipeActive; S.pipeActive = busy;
  if (wasBusy && !busy && S.section === "overview") {
    renderOverview(document.getElementById("main"));
  }
});
