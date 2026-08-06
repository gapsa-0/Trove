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
  fmtBytes,
} from "./dom.js";
import {
  S, TYPE_COL, TYPE_ICON, typeLabel,
} from "./state.js";
import {
  setGallery,
} from "./gallery.js";
import {
  openItem,
} from "./item.js";

const DUP_PAGE_SIZE = 40;
// What an archive with nothing grouped yet gets in place of the list: the
// same head, stats and status row as a full one, so the screen keeps its
// shape (People and Pets do the same) rather than swapping itself out for a
// centred emoji the moment there is nothing to show.
const DUP_EMPTY = `<div class="muted">No duplicate groups yet.</div>
  <div class="muted" style="margin-top:6px">Groups byte-identical copies and visually identical image exports (such as re-compressed JPG/PNG/HEIC files); nothing is ever deleted, extra copies are just hidden from browsing.</div>`;
// The filter matched nothing. Says which filter, because the archive plainly
// does have groups -- the tiles above are still counting them -- so "no
// groups" alone would read as a screen that had lost them.
const DUP_FILTERED_EMPTY = `<div class="muted">No groups hold a copy of that kind. Switch the filter back to <b>All groups</b> to see the rest.</div>`;
// How many groups the archive holds, unfiltered: the denominator the filtered
// count is read against. Set once per render from the summary, which is the
// same number the "Duplicate groups" tile shows.
let dupTotalGroups = 0;
export async function renderDedup(m) {
  const gen = S.nav, root = S.arch.id;
  const ds = await jget("/api/dups/summary?root=" + root);
  if (gen !== S.nav) return;
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Duplicates</h2><p>Review redundant copies Archive has safely hidden from your main library.</p></div>${docsButton("dups")}</div>
    <div class="statrow">
      <div class="stat"><div class="k">Unique files</div><div class="v">${(ds.unique || 0).toLocaleString()}</div></div>
      <div class="stat"><div class="k">Duplicate groups</div><div class="v">${ds.groups.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Redundant copies</div><div class="v">${ds.duplicates.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Reclaimable</div><div class="v">${fmtBytes(ds.reclaimable)}</div></div>
    </div>
    <div class="panel">${dedupStatusRow(ds)}</div>
    ${dupBreakdownPanel(ds)}
    ${ds.groups ? `<div class="muted" style="margin-bottom:12px">Exact copies and visually identical image exports. The <span style="color:var(--good)">✓ kept</span> copy stays in Browse; the rest are hidden (never deleted).</div>
    ${dupControls()}` : ""}
    <div id="dupgroups">${ds.groups ? "" : DUP_EMPTY}</div>
    <div class="infinite-status" id="dup-sentinel" aria-live="polite"></div>`;
  if (!ds.groups) return;
  dupTotalGroups = ds.groups;
  loadDupGroups();
}
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
   in loadMore rather than appended under the new filter. */
export function applyDupFilters() {
  loadDupGroups();
}
function loadDupGroups() {
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
    onPage: (groups, { first }) => {
      const wrap = document.getElementById("dupgroups");
      if (first) wrap.innerHTML = groups.length ? "" : DUP_FILTERED_EMPTY;
      groups.forEach(g => wrap.appendChild(dupGroupRow(g)));
    },
  });
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
// to press here -- so this only reports how much of the archive the last
// successful grouping run has already accounted for. "Unique files" is the
// same population the tile above counts: a group's copies are compared once,
// as one file, not once each.
function dedupStatusRow(ds) {
  const pending = ds.pending || 0;
  if (pending > 0) {
    return `<div class="d pending"><span class="dot pending"></span>${pending.toLocaleString()} unique file${pending === 1 ? "" : "s"} pending; duplicate detection runs automatically.</div>`;
  }
  return `<div class="d ok"><span class="dot ok"></span>All unique files compared.</div>`;
}
// What the redundant copies actually ARE. "27,318
// duplicates" hides two things worth knowing: how many are byte-identical
// (safe, boring) versus only visually the same (a re-compressed export,
// where the kept copy is a judgement call), and whether the space is going
// to photos or to a much smaller number of videos. Same bar/legend
// vocabulary as the Overview's storage panel, and the media colours are the
// shared TYPE_COL so a hue means the same thing on every screen.
const DUP_MATCH_LABEL = { identical: "Identical copies", visual: "Visual matches" };
const DUP_MATCH_COL = { identical: "#30d158", visual: "#5b8cff" };
function dupBreakdownPanel(ds) {
  const rows = [
    { label: "By match", items: ds.by_match,
      name: k => DUP_MATCH_LABEL[k] || k, colour: k => DUP_MATCH_COL[k] || "#8e8e93" },
    { label: "By media", items: ds.by_media,
      name: k => typeLabel(k), colour: k => TYPE_COL[k] || TYPE_COL.other },
  ].filter(r => (r.items || []).length);
  if (!rows.length) return "";       // older payload, or nothing to break down
  const total = ds.duplicates || 1;
  const section = r => `<div class="type-bar-row">
      <span class="type-bar-label">${r.label}</span>
      <div class="type-summary-bar">${r.items.map(i =>
    `<div class="type-summary-segment" style="width:${100 * i.count / total}%;background:${r.colour(i.key)}" title="${r.name(i.key)}: ${i.count.toLocaleString()} · ${fmtBytes(i.bytes)}"></div>`
  ).join("")}</div></div>
    <div class="type-summary-legend">${r.items.map(i =>
    `<span><span class="type-summary-key" style="background:${r.colour(i.key)}"></span>${r.name(i.key)} <span class="muted">· ${i.count.toLocaleString()} (${(100 * i.count / total).toFixed(1)}%) · ${fmtBytes(i.bytes)}</span></span>`
  ).join("")}</div>`;
  return `<div class="panel dup-breakdown"><div class="panel-heading"><div>
      <h3>What the copies are</h3><p>Every redundant copy, split two ways</p></div></div>
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
/* What the copies weigh, without claiming they all weigh the same. Only an
   exact group is copies of one size; a perceptual group is as often a big
   original beside two re-compressed exports, where "218.1 KB each" is both
   false and visibly at odds with the reclaimable figure next to it. Compared
   on the FORMATTED sizes so the answer matches what is on screen: two copies a
   few bytes apart round to one number, and "107.1 KB–107.1 KB" would be a
   worse way of saying "each". */
function dupSizes(members) {
  const sizes = (members || []).map(m => m.size || 0).sort((a, b) => a - b);
  if (!sizes.length) return "";
  const lo = fmtBytes(sizes[0]), hi = fmtBytes(sizes[sizes.length - 1]);
  return lo === hi ? `${lo} each` : `${lo}–${hi}`;
}
function dupGroupRow(g) {
  const row = document.createElement("div"); row.className = "dupgroup";
  const head = `<div class="dghead"><b>${g.count}×</b> · ${dupSizes(g.members)} ·
      <span class="muted">${fmtBytes(g.reclaimable)} reclaimable</span></div>`;
  const tiles = g.members.map(mm => {
    const kept = mm.role === "canonical";
    const tag = kept ? '<span class="keep">✓ kept</span>'
      : mm.match_type === "identical" ? '<span class="duptag exact">Identical copy</span>'
        : '<span class="duptag visual">Visual match</span>';
    const thumb = (mm.type === "image" || mm.type === "video") ? `<img src="/thumb/${mm.id}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'${TYPE_ICON[mm.type] || "📦"}'}))">`
      : `<div class="ph">${TYPE_ICON[mm.type] || "📦"}</div>`;
    return `<div class="duptile ${kept ? 'kept' : ''}" data-file-id="${mm.id}" title="${mm.folder}" onclick="openDupCopy(${mm.id})">
        ${thumb}<div class="dtcap">${tag}</div>
        <div class="dtpath">${mm.folder || '/'}</div></div>`;
  }).join("");
  row.innerHTML = head + `<div class="duprow">${tiles}</div>`;
  return row;
}
