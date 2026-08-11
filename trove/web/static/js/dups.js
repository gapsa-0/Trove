// The Duplicates screen: the summary stats, the breakdown of what the redundant
// copies are, and one row per group.

import {
  startInfiniteList,
} from "./infinite.js";
import {
  jget,
} from "./api.js";
import {
  docsButton,
} from "./docs.js";
import {
  esc, fmtBytes,
} from "./dom.js";
import {
  S, TYPE_COL, typeLabel,
} from "./state.js";
import {
  setGallery,
} from "./gallery.js";
import {
  openItem,
} from "./item.js";
import {
  onSnapshot,
} from "./pipeline.js";
import {
  thumbNode,
} from "./tiles.js";
import {
  setStat, why,
} from "./statwhy.js";

const DUP_PAGE_SIZE = 40;
// What an archive with nothing grouped yet gets in place of the list: the
// same head, stats and status row as a full one, so the screen keeps its
// shape (People and Pets do the same) rather than swapping itself out for a
// centred emoji the moment there is nothing to show.
const DUP_EMPTY = `<div class="muted">No copies found yet.</div>
  <div class="muted" style="margin-top:6px">Trove groups files that are byte-identical, and pictures that are the same shot saved differently. Nothing is ever deleted — the extra copies are hidden from Browse and listed here.</div>`;
// The filter matched nothing. Says which filter, because the archive plainly
// does have groups -- the tiles above are still counting them -- so "no
// groups" alone would read as a screen that had lost them.
const DUP_FILTERED_EMPTY = `<div class="muted">No groups hold a copy of that kind. Switch the filter back to <b>All groups</b> to see the rest.</div>`;
// How many groups the archive holds, unfiltered: the denominator the filtered
// count is read against. Set on every summary, which is the same number the
// "Duplicate groups" tile shows.
let dupTotalGroups = 0;
// The group total the list currently on screen was built for. Grouping is
// rebuilt wholesale on every run, so while one is going the answer can change
// on every tick -- and rebuilding the list under someone's fingers each time is
// worse than a list a few seconds old. The numbers tick live; the list is
// rebuilt at the two moments it is settled: when the run finishes, and when the
// screen is returned to. `null` means no list has been drawn.
let dupListTotal = null;

export async function renderDedup(m) {
  const gen = S.nav, root = S.arch.id;
  const ds = await jget("/api/dups/summary?root=" + root);
  if (gen !== S.nav) return;
  // One cached summary for both screens that read it: the Overview fills the
  // same slot from the same endpoint, so neither can be drawing from a payload
  // the other has already replaced.
  S.dupsum = ds;
  dupTotalGroups = ds.groups;
  dupListTotal = ds.groups ? ds.groups : null;
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Duplicates</h2><p>Every copy Trove found, and which one it keeps in Browse.</p></div>${docsButton("dups")}</div>
    <div class="statrow">
      <div class="stat"><div><div class="k">Unique files</div><div class="v" id="dup-unique">${(ds.unique || 0).toLocaleString()}</div></div>
        ${why("Unique files", (ds.unique || 0).toLocaleString(), "Every file you have, counting each group of copies only once.")}</div>
      <div class="stat"><div><div class="k">Duplicate groups</div><div class="v" id="dup-groups">${ds.groups.toLocaleString()}</div></div>
        ${why("Duplicate groups", ds.groups.toLocaleString(), "Sets of files found to be the same thing. One is kept; the rest are copies.")}</div>
      <div class="stat"><div><div class="k">Redundant copies</div><div class="v" id="dup-copies">${ds.duplicates.toLocaleString()}</div></div>
        ${why("Redundant copies", ds.duplicates.toLocaleString(), "The extra copies inside those groups. Still on disk, hidden from Browse.")}</div>
      <div class="stat"><div><div class="k">Reclaimable</div><div class="v" id="dup-reclaimable">${fmtBytes(ds.reclaimable)}</div></div>
        ${why("Reclaimable", fmtBytes(ds.reclaimable), "What those copies weigh together. Trove never deletes them; this is what you would get back if you did.")}</div>
    </div>
    <div class="panel" id="dup-status">${dedupStatusRow(ds)}</div>
    <div id="dup-breakdown">${dupBreakdownPanel(ds)}</div>
    ${ds.groups ? `<p class="dup-lede">Each group is one thing you have more than once. The <span class="dup-lede-kept">✓ Kept</span> copy is the one Browse shows; the rest are hidden, never deleted. To free the space, delete them yourself — every copy below says which folder it is in.</p>
    ${dupControls()}` : ""}
    <div id="dupgroups">${ds.groups ? "" : DUP_EMPTY}</div>
    <div class="infinite-status" id="dup-sentinel" aria-live="polite"></div>`;
  if (!ds.groups) return;
  loadDupGroups();
}

/* Redraw the figures from a fresh summary, without rebuilding the screen.

   Everything here writes into an id that renderDedup laid down, so scroll
   position, the pages the list has already loaded and both filter controls all
   survive a refresh -- which is the whole point of patching rather than
   re-rendering. The one thing that cannot be patched is the empty/populated
   edge: those two screens differ in furniture (the controls, the line above
   them) and not only in numbers, so crossing it is a re-render. */
async function refreshDedup(rebuildList = false) {
  if (S.section !== "dups" || !S.arch) return;
  if (!document.getElementById("dup-status")) return;   // not on screen
  const gen = S.nav, root = S.arch.id;
  let ds;
  try { ds = await jget("/api/dups/summary?root=" + root); }
  catch { return; }                     // transient; the next snapshot retries
  if (gen !== S.nav || S.section !== "dups") return;
  const main = document.getElementById("main");
  if (!document.getElementById("dup-status") || !main) return;
  S.dupsum = ds;
  dupTotalGroups = ds.groups;
  const drewList = dupListTotal !== null;
  if (!!ds.groups !== drewList) { renderDedup(main); return; }
  setStat("dup-unique", (ds.unique || 0).toLocaleString());
  setStat("dup-groups", ds.groups.toLocaleString());
  setStat("dup-copies", ds.duplicates.toLocaleString());
  setStat("dup-reclaimable", fmtBytes(ds.reclaimable));
  document.getElementById("dup-status").innerHTML = dedupStatusRow(ds);
  const breakdown = document.getElementById("dup-breakdown");
  if (breakdown) breakdown.innerHTML = dupBreakdownPanel(ds);
  if (rebuildList && ds.groups && dupListTotal !== ds.groups) loadDupGroups();
}

/* Keep the screen honest while grouping runs.

   Dedup is the one stage with nothing to press, which is exactly why this
   matters: someone who opens this screen during the first run of a big archive
   has no reason to think the numbers in front of them have stopped being true.
   They used to stay wrong for the rest of the session -- the sidebar said "Up
   to date" and the Overview said what had been found, while this screen still
   read "0" and "no copies found yet", and returning to it restored the same
   stale paint out of the section cache rather than asking again.

   Subscribed at module level, like the Overview's, because the fetch behind it
   belongs to pipeline.js and runs for the whole session anyway; the guards
   above are what a per-section subscription would have amounted to. */
onSnapshot(snap => {
  // Paused counts as not busy: nothing can move, so there is nothing to ask
  // about. "checking" is the disk walk, which has not answered yet.
  const busy = !["idle", "checking"].includes(snap.overall) && !snap.paused;
  const wasBusy = S.dupsActive; S.dupsActive = busy;
  if (S.section !== "dups") return;
  // The list is left alone while work runs and rebuilt once on the settling
  // edge -- see dupListTotal.
  if (busy || wasBusy) refreshDedup(!busy);
});

/* Returning to the screen asks again.

   The shell keeps a section's DOM alive while the user is elsewhere and replays
   it on the way back, so without this a screen stashed mid-run comes back
   exactly as stale as it was left. Called from the router's resumeSection. */
export function resumeDedup() { refreshDedup(true); }

/* The listing's two controls, in the vocabulary every other screen uses for
   the same job: plain selects on a filterbar (Timeline's year/month/place row
   is the same markup), not a bespoke set of toggles.

   Reading order follows what they do to the list -- which groups, then in what
   order -- and both are one control each, so neither has to be read twice to
   find out what it currently says. The counter sits after them because it
   describes their result, and it is the only thing here that changes on its
   own. */
function dupControls() {
  return `<div class="filterbar dup-filterbar">
      <select class="fsel" id="dup-match" aria-label="Filter duplicate groups" onchange="applyDupFilters()">
        <option value="">All groups</option>
        <option value="identical">With identical copies</option>
        <option value="visual">With visual matches</option>
      </select>
      <select class="fsel" id="dup-sort" aria-label="Sort duplicate groups" onchange="applyDupFilters()">
        <option value="">Biggest saving first</option>
        <option value="count_desc">Most copies first</option>
        <option value="count_asc">Fewest copies first</option>
      </select>
      <span class="muted dup-count" id="dup-count" aria-live="polite"></span>
    </div>`;
}
/* Re-run the listing under whatever the two controls now say.

   A fresh startInfiniteList rather than a filtered redraw of what is loaded:
   the list is paged, so the groups matching a filter are mostly still on the
   server. Restarting resets the observer and offset together -- the old state
   object is dropped, and its in-flight page is discarded by the identity check
   in loadMore rather than appended under the new filter.

   Then back to the top of the list. Changing a filter replaces every row below
   the controls, so leaving the scroll where it was landed the reader in the
   middle of a list they had not seen the start of, with the controls they had
   just used somewhere off screen above them. */
export function applyDupFilters() {
  loadDupGroups();
  const bar = document.querySelector(".dup-filterbar"), main = document.getElementById("main");
  if (bar && main) main.scrollTop += bar.getBoundingClientRect().top - main.getBoundingClientRect().top - 12;
}
function loadDupGroups() {
  dupListTotal = dupTotalGroups;
  const query = () => {
    const p = new URLSearchParams({ root: S.arch.id, limit: DUP_PAGE_SIZE });
    const match = document.getElementById("dup-match")?.value;
    const sort = document.getElementById("dup-sort")?.value;
    if (match) p.set("match", match);
    if (sort) p.set("sort", sort);
    return p;
  };
  startInfiniteList("dupList", {
    sentinelId: "dup-sentinel", pageSize: DUP_PAGE_SIZE,
    fetchPage: async offset => {
      const p = query(); p.set("offset", offset);
      const res = await jget("/api/dups?" + p);
      dupCount(res.total);
      return res.groups;
    },
    onPage: (groups, { first, done }) => {
      const wrap = document.getElementById("dupgroups");
      if (first) wrap.innerHTML = groups.length ? "" : DUP_FILTERED_EMPTY;
      groups.forEach(g => wrap.appendChild(dupGroupRow(g)));
      if (done) markEndOfList(wrap);
    },
  });
}
/* Say that the list has ended.

   The sentinel empties itself when a page lands, so a list that has reached its
   last group and one whose next page failed to arrive look identical to someone
   who has just scrolled to the bottom. Written into the list rather than the
   sentinel because the sentinel is cleared right after onPage returns. */
function markEndOfList(wrap) {
  const n = wrap.querySelectorAll(".dupgroup").length;
  if (!n || wrap.querySelector(".dup-end")) return;
  const end = document.createElement("p");
  end.className = "muted dup-end";
  end.textContent = `That’s all ${n.toLocaleString()} group${n === 1 ? "" : "s"}.`;
  wrap.appendChild(end);
}
/* What the filter is showing, in the same words as the tile above it.

   Only when a filter is on: unfiltered, this would repeat the "Duplicate
   groups" tile back at the reader, and a number that never disagrees with
   another number on the same screen is not worth the row it sits in. */
function dupCount(shown) {
  const el = document.getElementById("dup-count");
  if (!el) return;
  const filtered = !!document.getElementById("dup-match")?.value;
  el.textContent = filtered
    ? `${(shown || 0).toLocaleString()} of ${dupTotalGroups.toLocaleString()} group${dupTotalGroups === 1 ? "" : "s"}`
    : "";
}
// One-line status, the same shape People and Pets get from detectStatusRow:
// no progress bar, no emoji, exactly one row so the panel never reserves
// empty space. Dedup is started by the scheduler alone -- there is nothing
// to press here -- so this reports what the stage is doing and how much of the
// archive the last successful grouping run has already accounted for. The
// pending count is in unique files, the same population the tile above counts:
// a group's copies are compared once, as one file, not once each. The finished
// line drops the word -- once nothing is pending there is no distinction left
// to draw, and "all unique files compared" invited the reading that some other
// files were not.
const dedupStage = () => ((S.pipeline && S.pipeline.stages) || []).find(s => s.id === "dedup");
function dedupStatusRow(ds) {
  const pending = ds.pending || 0;
  const stage = dedupStage();
  // Said plainly while it is happening. The figures on this screen move on
  // their own only in this state, and a reader who is not told that reads a
  // number that is still climbing as the answer.
  if (stage && stage.state === "running") {
    return `<div class="d pending"><span class="dot pending"></span>Comparing files now. These figures update as it goes.</div>`;
  }
  if (pending > 0) {
    return `<div class="d pending"><span class="dot pending"></span>${pending.toLocaleString()} unique file${pending === 1 ? "" : "s"} still to compare; duplicate detection runs automatically.</div>`;
  }
  return `<div class="d ok"><span class="dot ok"></span>All files compared.</div>`;
}
// What the redundant copies actually ARE. "27,318
// duplicates" hides two things worth knowing: how many are byte-identical
// (safe, boring) versus only visually the same (a re-compressed export,
// where the kept copy is a judgement call), and whether the space is going
// to photos or to a much smaller number of videos. Same bar/legend and the
// same Size/Files switch as the Overview's storage panel, and the media
// colours are the shared TYPE_COL so a hue means the same thing on every
// screen.
//
// The switch is not decoration. These bars used to be drawn by count alone,
// which cannot answer the question this panel exists for: four re-encoded 4K
// videos and four thumbnails are the same four segments by count and nowhere
// near the same news about disk space. Size is the default here because
// "Reclaimable" is the figure the screen opens with.
//
// The last row is the other half of that question -- how much of each type
// survives deduplication -- and it is deliberately measured against its OWN
// total. Its bar sums to `unique` (the tile above, one file per group) while
// the two copy rows sum to `duplicates`, so segments must not be read across
// rows; the label says so and a rule separates it for exactly that reason.
const DUP_MATCH_LABEL = { identical: "Identical copies", visual: "Visual matches" };
const DUP_MATCH_COL = { identical: "#30d158", visual: "#5b8cff" };
const DUP_METRICS = {
  size: { label: "Size", value: i => i.bytes || 0, text: i => fmtBytes(i.bytes || 0) },
  files: { label: "Files", value: i => i.count, text: i => i.count.toLocaleString() },
};
const dupMetric = () => (DUP_METRICS[S.dupMetric] ? S.dupMetric : "size");
export function setDupMetric(key) {
  S.dupMetric = key;
  const el = document.getElementById("dup-breakdown");
  if (el && S.dupsum) el.innerHTML = dupBreakdownPanel(S.dupsum);
}
function dupBreakdownPanel(ds) {
  const key = dupMetric(), metric = DUP_METRICS[key];
  const mediaName = k => typeLabel(k), mediaCol = k => TYPE_COL[k] || TYPE_COL.other;
  // Sorted by whichever metric is on screen, so a bar reads biggest-first
  // whichever way the switch is set and its legend reads in the same order --
  // the server ranks by count, which under "Size" drew a segment smaller than
  // the one after it. Match is the exception and keeps the server's fixed
  // identical-then-visual order: those two are a pair with a fixed pair of
  // colours, and letting them swap places as the figures move is how a colour
  // stops meaning one thing.
  const ranked = items => [...items].sort((a, b) => metric.value(b) - metric.value(a));
  const rows = [
    { label: "Copies, by match", items: ds.by_match,
      name: k => DUP_MATCH_LABEL[k] || k, colour: k => DUP_MATCH_COL[k] || "#8e8e93" },
    { label: "Copies, by media", items: ranked(ds.by_media || []), name: mediaName, colour: mediaCol },
    { label: "Unique files, by media", items: ranked(ds.by_unique || []),
      name: mediaName, colour: mediaCol, apart: true },
  ].filter(r => (r.items || []).length);
  if (!rows.length) return "";       // older payload, or nothing to break down
  // A row's own total, so each bar is a whole and no reader has to work out
  // which denominator it is looking at.
  const share = (item, total) => {
    const p = 100 * metric.value(item) / (total || 1);
    return p > 0 && p < 0.1 ? "<0.1%" : `${p.toFixed(1)}%`;
  };
  const section = r => {
    const total = r.items.reduce((sum, i) => sum + metric.value(i), 0);
    return `<div class="type-bar-row${r.apart ? " type-bar-apart" : ""}">
      <span class="type-bar-label">${r.label}</span>
      <div class="type-summary-bar">${r.items.map(i =>
      `<div class="type-summary-segment" style="width:${100 * metric.value(i) / (total || 1)}%;background:${r.colour(i.key)}" title="${esc(r.name(i.key))}: ${i.count.toLocaleString()} · ${fmtBytes(i.bytes)}"></div>`
    ).join("")}</div></div>
    <div class="type-summary-legend">${r.items.map(i =>
      `<span><span class="type-summary-key" style="background:${r.colour(i.key)}"></span>${esc(r.name(i.key))} <span class="muted">· ${metric.text(i)} (${share(i, total)})</span></span>`
    ).join("")}</div>`;
  };
  // A single-segment bar is a rectangle that says 100%: no comparison in it,
  // and three labels above it saying so. An archive with one kind of file in
  // it gets the sentence instead.
  const only = rows.length === 1 && rows[0].items.length === 1;
  if (only) {
    const i = rows[0].items[0];
    return `<div class="panel dup-breakdown"><div class="panel-heading"><div>
        <h3>What the archive holds</h3>
        <p>${i.count.toLocaleString()} ${typeLabel(i.key)} file${i.count === 1 ? "" : "s"}, ${fmtBytes(i.bytes)}, and nothing else yet.</p>
      </div></div></div>`;
  }
  // Headed by what the panel actually holds: with no groups yet the two copy
  // rows are empty and only the unique breakdown is left, and calling that
  // "what the copies are" would describe a row that is not about copies.
  const dups = rows.some(r => !r.apart);
  const other = key === "size" ? "files" : "size";
  const sw = Object.entries(DUP_METRICS).map(([id, m]) =>
    `<button type="button" class="${id === key ? "on" : ""}"
        onclick="setDupMetric('${id === key ? other : id}')">${m.label}</button>`).join("");
  return `<div class="panel dup-breakdown"><div class="panel-heading"><div>
      <h3>${dups ? "What the copies are" : "What the archive holds"}</h3>
      <p>${dups
    ? "Every redundant copy, split two ways, beside the unique files they sit among"
    : "Unique files by media"}</p></div>
      <div class="metric-switch" id="dup-metric-switch">${sw}</div></div>
    ${rows.map(section).join("")}</div>`;
}
/* Open a copy with the arrows bounded by ITS group.

   The viewer walks S.gallery, so what goes in it is a claim about what "next"
   means. Every copy on the screen was the wrong claim: the arrows ran off the
   end of the group you were comparing and into the next group's photographs,
   which are a different picture entirely. A group is the set this screen exists
   to compare, so it is the set the arrows should stay inside.

   Read off the group's own tiles rather than passed in as ids, for the reason
   galleryFromGrid gives: the list pages and rebuilds, and a parallel list would
   drift the first time a redraw was missed. */
export function openDupCopy(id) {
  const group = document.querySelector(`.duptile[data-file-id="${id}"]`)?.closest(".dupgroup");
  if (group) {
    setGallery(
      [...group.querySelectorAll("[data-file-id]")].map(el => Number(el.dataset.fileId)),
      "in this duplicate group"
    );
  }
  openItem(id);
}
// The last two segments of a folder, which is where copies of one file
// actually differ: `Takeout/Google Photos/2019 - Photos` and
// `Old drive/backup 2018/pictures` are told apart by their ends, and so are
// `…/Bariloche - dia 1` and `…/Bariloche - dia 2`. The caption used to carry
// the whole path cut off at whatever fitted in 120 pixels, which on a deep
// folder meant every copy in a group read `Takeout/Google Photos/…` and the
// screen said nothing at all about which file was which. The whole path is
// still one hover away, on the tile.
const ROOT_FOLDER = "the archive root";
function shortFolder(folder) {
  if (!folder) return ROOT_FOLDER;
  const parts = folder.split("/").filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : parts.join("/");
}
// A file name cut in the middle rather than at the end, because both ends of it
// carry the answer. `IMG_20190812_143022.jpg` and `IMG_20190812_143055.jpg` are
// one group's two copies and differ in the last four characters before the
// extension -- the exact part a plain CSS ellipsis throws away, leaving two
// tiles reading `IMG_2019081…`. The whole name is on the tile's tooltip, and
// the CSS ellipsis stays as the backstop for a name with no room even for this.
const NAME_MAX = 22;
function shortName(name) {
  if (name.length <= NAME_MAX) return name;
  const tail = Math.ceil((NAME_MAX - 1) / 2);
  return name.slice(0, NAME_MAX - 1 - tail) + "…" + name.slice(-tail);
}
/* How many copies, and what freeing them gives back. Deliberately no per-copy
   size: only an exact group is copies of one size, and stating a range for the
   rest spent a third of the header line on a number nobody acts on. What the
   header is for is deciding whether a group is worth opening, and the count
   and the saving answer that on their own. Opening a copy still states its own
   size (panel.js's subline), which is where a size settles anything.

   The count is written out. "9×" left it to the reader to guess whether nine
   was the files or the copies to be rid of; the group has nine files and eight
   of them are spare, and both numbers are on the line the saving is on. */
function dupGroupRow(g) {
  const row = document.createElement("div"); row.className = "dupgroup";
  const spare = Math.max(0, g.count - 1);
  const head = document.createElement("div"); head.className = "dghead";
  head.innerHTML = `<b>${g.count} copies</b> <span class="muted">· ${spare} spare · ${fmtBytes(g.reclaimable)} reclaimable</span>`;
  const strip = document.createElement("div"); strip.className = "duprow";
  g.members.forEach(mm => strip.appendChild(dupTile(mm)));
  row.append(head, strip);
  return row;
}
// The words on a copy, and the one on the file that is kept. Filled rather than
// quiet for "Kept": the kept copy is the one fact a reader is looking for in a
// row of identical pictures, and it used to be the faintest thing in it while
// eight bright pills marked the copies. The vocabulary is the inspector's,
// which shows the same group from the other side (panel.js's COPY_TAG).
const DUP_TAG = { canonical: "✓ Kept", identical: "Identical copy", visual: "Visual match" };
/* One copy: what it looks like, what makes it a copy, and where it lives.

   A button, not a div with an onclick. Every other grid in the app is built out
   of tiles.js's `tile()`, which is a real control -- reachable by keyboard,
   announced with the file's name -- and this screen's hand-rolled copy was
   neither, which left 150 pictures on a page that a keyboard could not reach at
   all. It also interpolated the folder straight into markup, so a folder with a
   quotation mark in its name ("Fotos de "Mama"") broke out of the title
   attribute, took the onclick with it and printed the leftover as text. Both
   are the same lesson: this is the same object every other screen draws, and it
   is built the same way. */
function dupTile(mm) {
  const kept = mm.role === "canonical";
  const name = mm.name || "";
  const where = mm.folder ? `${mm.folder}/${name}` : name;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "duptile" + (kept ? " kept" : "");
  b.dataset.fileId = mm.id;
  b.title = where;
  b.setAttribute("aria-label", `${name}, ${DUP_TAG[mm.match_type] || mm.match_type}, in ${mm.folder || ROOT_FOLDER}`);
  b.onclick = () => openDupCopy(mm.id);
  b.appendChild(thumbNode(mm));
  const cap = document.createElement("div"); cap.className = "dtcap";
  cap.innerHTML = `<span class="duptag ${mm.match_type}">${DUP_TAG[mm.match_type] || esc(mm.match_type)}</span>`
    + (mm.type === "video" ? `<span class="dtplay" aria-hidden="true">▶</span>` : "");
  const file = document.createElement("div"); file.className = "dtname";
  file.textContent = shortName(name);
  const folder = document.createElement("div"); folder.className = "dtpath";
  folder.textContent = shortFolder(mm.folder);
  b.append(cap, file, folder);
  return b;
}
