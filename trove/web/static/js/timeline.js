// The Timeline screen: the hand-drawn canvas chart, its filter bar, and the
// "how dates were found" bar that sits beneath it. The chart is drawn rather
// than delegated to a charting library, so the path maths lives here too.

import {
  MONTH_NAMES, selVal,
} from "./library.js";
import {
  checkedPeople, clearPeopleChecks, peopleFilterHTML, setGroupChecks, updatePeopleFilterLabel,
} from "./group-filter.js";
import {
  jget,
} from "./api.js";
import {
  docsButton,
} from "./docs.js";
import {
  esc,
} from "./dom.js";
import {
  S, typeColour,
} from "./state.js";

// Same five sources the file panel names, in the shorter form a legend wants.
// "Embedded EXIF" was this chart's own wording for the key the panel called
// "From the camera", and both were wrong in the same way: the key covers any
// timestamp a file carries about itself, and a PDF carries one without ever
// having been near a camera or an EXIF block.
const DATE_SRC_LABEL = {
  takeout_json: "Google Takeout JSON", exif: "File metadata",
  filename: "Filename pattern", mtime: "File modified time", unknown: "Unresolved source",
  unresolved: "No date found"
};
const DATE_SRC_COL = {
  takeout_json: "#5b9dff", exif: "#c77dff", filename: "#57c98b",
  mtime: "#e6b45e", unknown: "#9aa3b2", unresolved: "#4a5261"
};
async function renderDateSourceBar() {
  const el = document.getElementById("dsbar"); if (!el) return;
  const r = await jget("/api/dates/sources?root=" + S.arch.id);
  const rows = r.sources.slice();
  if (r.undated > 0) rows.push({ source: "unresolved", count: r.undated });
  const total = r.total || 1;
  const pct = x => (100 * x.count / total).toFixed(1);
  const colour = x => DATE_SRC_COL[x.source] || "#8b93a3";
  const label = x => DATE_SRC_LABEL[x.source] || x.source;
  const segs = rows.map(x =>
    `<div style="width:${100 * x.count / total}%;background:${colour(x)}" data-tip="${label(x)}: ${pct(x)}%"></div>`
  ).join("");
  const legend = rows.map(x =>
    `<span><span class="tl-swatch" style="background:${colour(x)}"></span>${label(x)}<span class="muted">, ${pct(x)}%</span></span>`
  ).join("");
  el.innerHTML = `<div class="tl-stack">${segs}</div><div class="tl-legend">${legend}</div>`;
}
export async function renderTimeline(m) {
  const gen = S.nav;
  S.timeline = { bucket: "month", year: "", month: "", people: [], place: "" };
  m.innerHTML = `<div class="pagehead"><div><h2 class="sec">Timeline</h2>
      <p>See how your archive grows over time, then narrow it by date, people together, or place.</p></div>${docsButton("timeline")}</div>
    <div class="filterbar" id="tl-filterbar"></div>
    <div class="tl-legend" id="tllegend"></div>
    <canvas id="tlc2" width="1180" height="380"></canvas>
    <p class="tl-note">Running total of files over time, each type scaled to its own final count.</p>
    <h2 class="sec tl-second">How dates were found</h2>
    <div class="panel" id="dsbar">Loading…</div>`;
  await buildTimelineFilterBar();
  if (gen !== S.nav) return;
  await Promise.all([drawTimeline("month"), renderDateSourceBar()]);
}
async function buildTimelineFilterBar() {
  const gen = S.nav;
  const f = await jget("/api/browse/filters?root=" + S.arch.id);
  if (gen !== S.nav) return;
  const bar = document.getElementById("tl-filterbar"); if (!bar) return;
  const years = [...new Set((f.periods || []).map(p => p.slice(0, 4)))];
  const opt = (v, l) => `<option value="${v}">${l}</option>`;
  const parts = [];
  if (years.length)
    parts.push(`<select class="fsel" id="tl-year-filter" onchange="onTimelineYearChange()">` +
      opt("", "All years") + years.map(y => opt(y, y)).join("") + `</select>` +
      `<select class="fsel" id="tl-month-filter" onchange="applyTimelineFilters()" disabled>` +
      opt("", "All months") + `</select>`);
  parts.push(peopleFilterHTML("tl", f.people || []));
  parts.push(`<select class="fsel" id="tl-place-filter" onchange="applyTimelineFilters()" ${f.places && f.places.length ? "" : "disabled"} title="${f.places && f.places.length ? "Filter by place" : "Name places in Places to enable this filter"}">` +
    opt("", f.places && f.places.length ? "All places" : "No places named yet") + (f.places || []).map(p => opt(p.id, esc(p.name))).join("") + `</select>`);
  parts.push(`<button class="quietbtn sm" id="tl-clear" onclick="clearTimelineFilters()" style="display:none">Clear filters</button>`);
  bar.innerHTML = parts.join("");
  S.timelineOpts = f;
}
/* Which months the chosen year actually has, and which of them is picked.

   Split out of onTimelineYearChange so the resume below can rebuild the list
   without also announcing a change the user did not make. Mirrors the
   Library's setLibraryMonthOptions, down to taking the selection as an
   argument for exactly that reason. */
function setTimelineMonthOptions(year, selected = "") {
  const msel = document.getElementById("tl-month-filter"); if (!msel) return;
  if (!year) {
    msel.innerHTML = '<option value="">All months</option>';
    msel.disabled = true;
    return;
  }
  // .filter(Boolean): a year-only period ("2024", from a manual year-precision
  // date) yields an empty month slice, so drop it rather than offer a blank.
  const months = [...new Set((S.timelineOpts.periods || [])
    .filter(p => p.slice(0, 4) === year).map(p => p.slice(5, 7)).filter(Boolean))].sort();
  msel.innerHTML = '<option value="">All months</option>' +
    months.map(mm => `<option value="${mm}">${MONTH_NAMES[+mm - 1]}</option>`).join("");
  msel.disabled = false;
  msel.value = selected;
}
export function onTimelineYearChange() {
  setTimelineMonthOptions(selVal("tl-year-filter"));
  applyTimelineFilters();
}
/* Ask again on the way back to this screen.

   The shell sets a section's DOM aside while you are elsewhere and replays it
   when you return, which is what keeps the chart and the scroll -- and what
   left this filter bar as the list of people it was built from. Name three
   people in People, come back, and the filter still offered the names from
   before. Browse looked right only because its DOM is released rather than
   stashed, so it is rebuilt from nothing every time.

   The selections are put back from `S.timeline` rather than read off the
   markup being replaced. Anyone renamed away or hidden meanwhile is simply not
   there to re-check, and applyTimelineFilters folds that back into the filter
   -- which is the honest answer, rather than a chart narrowed by somebody the
   archive no longer has. */
export async function resumeTimeline() {
  if (!document.getElementById("tl-filterbar")) return;
  const { year, month, place } = S.timeline, people = (S.timeline.people || []).slice();
  await buildTimelineFilterBar();
  if (!document.getElementById("tl-filterbar")) return;
  const ysel = document.getElementById("tl-year-filter");
  if (ysel) ysel.value = year;
  setTimelineMonthOptions(ysel ? ysel.value : "", month ? month.slice(5, 7) : "");
  setGroupChecks("tl", "people", people);
  const psel = document.getElementById("tl-place-filter");
  if (psel) psel.value = place;
  applyTimelineFilters();
}
export function applyTimelineFilters() {
  const t = S.timeline;
  t.year = selVal("tl-year-filter");
  const mm = selVal("tl-month-filter");
  t.month = (t.year && mm) ? `${t.year}-${mm}` : "";
  t.people = checkedPeople("tl");
  t.place = selVal("tl-place-filter");
  updatePeopleFilterLabel("tl", S.timelineOpts.people || []);
  const clear = document.getElementById("tl-clear");
  if (clear) clear.style.display = (t.year || t.people.length || t.place) ? "" : "none";
  drawTimeline(t.bucket);
}
export function clearTimelineFilters() {
  ["tl-year-filter", "tl-place-filter"].forEach(id => {
    const e = document.getElementById(id); if (e) e.value = "";
  });
  clearPeopleChecks("tl");
  const msel = document.getElementById("tl-month-filter");
  if (msel) { msel.innerHTML = '<option value="">All months</option>'; msel.disabled = true; }
  applyTimelineFilters();
}
/* "2026-08" is the bucket's key, not a month anybody says out loud. */
function periodWords(period) {
  const m = /^(\d{4})-(\d{2})$/.exec(period || "");
  return m ? `${MONTH_NAMES[+m[2] - 1]} ${m[1]}` : (period || "");
}
function monotonePath(ctx, xs, ys) {
  // Fritsch-Carlson monotone cubic: smooth but never overshoots the data,
  // so a line can't dip below zero or spike between points.
  const n = xs.length;
  if (n === 1) { return; }
  const dx = [], slope = [];
  for (let i = 0; i < n - 1; i++) { dx[i] = xs[i + 1] - xs[i]; slope[i] = (ys[i + 1] - ys[i]) / dx[i]; }
  const t = new Array(n); t[0] = slope[0]; t[n - 1] = slope[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = (slope[i - 1] * slope[i] <= 0) ? 0 : (slope[i - 1] + slope[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / slope[i], b = t[i + 1] / slope[i], h = Math.hypot(a, b);
    if (h > 3) { const tau = 3 / h; t[i] = tau * a * slope[i]; t[i + 1] = tau * b * slope[i]; }
  }
  ctx.moveTo(xs[0], ys[0]);
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i];
    ctx.bezierCurveTo(xs[i] + h / 3, ys[i] + t[i] * h / 3, xs[i + 1] - h / 3, ys[i + 1] - t[i + 1] * h / 3, xs[i + 1], ys[i + 1]);
  }
}
// Draws one type's curve normalized to its own max (0..1), so each type's
// shape is comparable regardless of volume. Per-type totals live in the
// legend above the canvas, not inside the plot.
/* A canvas cannot inherit a colour, so the chart has to be told the theme's
   every time it draws. It used to hardcode #3a414e / #232833 gridlines and
   #9aa3b2 axis text -- values picked against the dark theme, which on the light
   theme's white card came out near-black and read as the data rather than the
   rule behind it. */
function chartInk() {
  const s = getComputedStyle(document.documentElement);
  const read = (name, fallback) => (s.getPropertyValue(name) || "").trim() || fallback;
  return {
    axis: read("--line-strong", "rgba(60,60,67,.23)"),
    grid: read("--line", "rgba(60,60,67,.14)"),
    text: read("--faint", "#8e8e93"),
  };
}
function drawTypeChart(canvasId, rows, ordered, maxByType) {
  const cv = document.getElementById(canvasId); if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, padL = 40, padR = 16, padB = 28, padT = 14;
  ctx.clearRect(0, 0, W, H);
  const n = rows.length, ink = chartInk();
  const X = i => padL + (n === 1 ? (W - padL - padR) / 2 : i * (W - padL - padR) / (n - 1));
  const Y = frac => H - padB - frac * (H - padT - padB);
  [0, 0.25, 0.5, 0.75, 1].forEach(frac => {
    const y = Y(frac);
    ctx.strokeStyle = frac === 0 ? ink.axis : ink.grid;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
  });
  ctx.font = "10px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.fillStyle = ink.text; ctx.fillText("0", padL - 8, Y(0));
  ordered.forEach(t => {
    const xs = rows.map((r, i) => X(i)), ys = rows.map(r => Y((r[t] || 0) / (maxByType[t] || 1)));
    ctx.beginPath(); monotonePath(ctx, xs, ys);
    ctx.lineTo(xs[n - 1], H - padB); ctx.lineTo(xs[0], H - padB); ctx.closePath();
    ctx.fillStyle = typeColour(t) + "1e"; ctx.fill();
    ctx.beginPath(); monotonePath(ctx, xs, ys);
    ctx.strokeStyle = typeColour(t); ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
  });
  ctx.fillStyle = ink.text; ctx.textAlign = "center"; ctx.textBaseline = "top";
  const step = Math.max(1, Math.ceil(n / 12));
  rows.forEach((r, i) => { if (i % step === 0 || i === n - 1) ctx.fillText(r.period, X(i), H - padB + 8); });
}
function drawTypeLegend(legendId, ordered, totalsByType) {
  const leg = document.getElementById(legendId); leg.innerHTML = "";
  ordered.forEach(t => {
    const sp = document.createElement("span");
    sp.innerHTML = `<span class="tl-swatch" style="background:${typeColour(t)}"></span>`
      + `${t}<span class="muted">, ${totalsByType[t].toLocaleString()}</span>`;
    leg.appendChild(sp);
  });
}
async function drawTimeline(bucket) {
  const t = S.timeline;
  t.bucket = bucket;
  const p = new URLSearchParams({ root: S.arch.id, bucket });
  if (t.year) p.set("year", t.year); if (t.month) p.set("month", t.month);
  t.people.forEach(id => p.append("person", id)); if (t.place) p.set("place", t.place);
  const { series } = await jget("/api/timeline?" + p);
  const cv2 = document.getElementById("tlc2");
  const leg = document.getElementById("tllegend");
  if (!cv2) return;
  // The caption under the canvas describes the plot, so it leaves with it.
  const note = document.querySelector(".tl-note");
  const say = words => {
    cv2.getContext("2d").clearRect(0, 0, cv2.width, cv2.height);
    cv2.hidden = true;
    if (note) note.hidden = true;
    leg.innerHTML = `<span class="muted">${words}</span>`;
  };
  cv2.hidden = false;
  if (note) note.hidden = false;
  if (!series.length) {
    const filtered = t.year || t.people.length || t.place;
    // "run Extract on the Overview tab" named a stage the Overview calls
    // Indexing, on a tab this app does not have.
    say(filtered ? "No dated media matches these filters."
      : "Nothing has a date yet. Indexing fills these in as it reads the folder.");
    return;
  }
  const types = ["image", "video", "audio"].filter(t => series.some(s => s[t]));
  const totalsByType = {};
  types.forEach(t => { totalsByType[t] = series.reduce((a, s) => a + (s[t] || 0), 0); });
  const ordered = types.slice().sort((a, b) => totalsByType[b] - totalsByType[a]);

  const running = Object.fromEntries(types.map(t => [t, 0]));
  const cumRows = series.map(s => {
    const row = { period: s.period };
    types.forEach(t => { running[t] += s[t] || 0; row[t] = running[t]; });
    return row;
  });
  const cumMax = {}; types.forEach(t => { cumMax[t] = Math.max(totalsByType[t], 1); });
  // One bucket is not a shape. Every series normalises to its own maximum, so
  // with a single period they all land on 1.0 and stack into one dot on a grid
  // of four unlabelled rules -- a plot that says less than the sentence it
  // costs a 380px canvas to draw.
  if (series.length < 2) {
    const counts = ordered.map(t =>
      `${totalsByType[t].toLocaleString()} ${t}${totalsByType[t] === 1 ? "" : "s"}`);
    say(`Everything dated so far falls in ${esc(periodWords(series[0].period))}: `
      + `${counts.join(", ")}. The chart needs a second month to draw a shape.`);
    return;
  }
  drawTypeChart("tlc2", cumRows, ordered, cumMax);
  drawTypeLegend("tllegend", ordered, totalsByType);
}
