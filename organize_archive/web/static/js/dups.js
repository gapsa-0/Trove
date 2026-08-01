// The Duplicates screen: the summary stats, the breakdown of what the redundant
// copies are, and one row per group.

import {
  jget,
} from "./api.js";
import {
  fmtBytes,
} from "./dom.js";
import {
  S, TYPE_COL, TYPE_ICON, typeLabel,
} from "./state.js";
import {
  startInfiniteList,
} from "./main.js";

const DUP_PAGE_SIZE = 40;
export async function renderDedup(m) {
  const gen = S.nav, root = S.arch.id;
  const ds = await jget("/api/dups/summary?root=" + root);
  if (gen !== S.nav) return;
  if (!ds.groups) {
    m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Duplicates</h2><p>Review redundant copies Archive has safely hidden from your main library.</p></div></div>
      <div class="soonbox"><div class="big">🧹</div>
      <p>No duplicates found yet.</p>
      <p class="muted">This runs automatically once files are scanned and dated. See the Overview tab for progress. Groups byte-identical copies and visually identical image exports (such as re-compressed JPG/PNG/HEIC files); nothing is ever deleted, extra copies are just hidden from browsing.</p></div>`;
    return;
  }
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Duplicates</h2><p>Review redundant copies Archive has safely hidden from your main library.</p></div></div>
    <div class="statrow">
      <div class="stat"><div class="k">Duplicate groups</div><div class="v">${ds.groups.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Redundant copies</div><div class="v">${ds.duplicates.toLocaleString()}</div></div>
      <div class="stat"><div class="k">Reclaimable</div><div class="v">${fmtBytes(ds.reclaimable)}</div></div>
    </div>
    ${dupBreakdownPanel(ds)}
    <div class="muted" style="margin-bottom:12px">Exact copies and visually identical image exports. The <span style="color:var(--good)">✓ kept</span> copy stays in Browse; the rest are hidden (never deleted). Biggest space first.</div>
    <div id="dupgroups"></div>
    <div class="infinite-status" id="dup-sentinel" aria-live="polite"></div>`;
  startInfiniteList("dupList", {
    sentinelId: "dup-sentinel", pageSize: DUP_PAGE_SIZE,
    fetchPage: async offset => {
      const res = await jget(`/api/dups?root=${S.arch.id}&offset=${offset}&limit=${DUP_PAGE_SIZE}`);
      return res.groups;
    },
    onPage: (groups, { first }) => {
      const wrap = document.getElementById("dupgroups");
      if (first) wrap.innerHTML = "";
      groups.forEach(g => wrap.appendChild(dupGroupRow(g)));
    },
  });
}
// What the redundant copies actually ARE (things_to_fix #35). "27,318
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
function dupGroupRow(g) {
  const row = document.createElement("div"); row.className = "dupgroup";
  const head = `<div class="dghead"><b>${g.count}×</b> · ${fmtBytes(g.size_each)} each ·
      <span class="muted">${fmtBytes(g.reclaimable)} reclaimable</span></div>`;
  const tiles = g.members.map(mm => {
    const kept = mm.role === "canonical";
    const tag = kept ? '<span class="keep">✓ kept</span>'
      : mm.match_type === "identical" ? '<span class="duptag exact">Identical copy</span>'
        : '<span class="duptag visual">Visual match</span>';
    const thumb = (mm.type === "image" || mm.type === "video") ? `<img src="/thumb/${mm.id}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'${TYPE_ICON[mm.type] || "📦"}'}))">`
      : `<div class="ph">${TYPE_ICON[mm.type] || "📦"}</div>`;
    return `<div class="duptile ${kept ? 'kept' : ''}" title="${mm.folder}" onclick="openItem(${mm.id})">
        ${thumb}<div class="dtcap">${tag}</div>
        <div class="dtpath">${mm.folder || '/'}</div></div>`;
  }).join("");
  row.innerHTML = head + `<div class="duprow">${tiles}</div>`;
  return row;
}
