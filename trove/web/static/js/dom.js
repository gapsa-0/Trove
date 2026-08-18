// Small DOM and formatting helpers, shared by every screen. Nothing here reads
// application state or talks to the server; if a helper needs either, it belongs
// with the screen that owns it.

// Whole bytes, one decimal above that: a tenth of a kilobyte is a real
// distinction and a tenth of a byte is not, so plain bytes are printed as
// integers. It is the zero case that made this worth fixing -- an archive with
// nothing to reclaim announced "0.0 B", which reads like a rounded-down
// quantity rather than none at all.
export function fmtBytes(n) {
  if (n == null) return "-"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0, f = n;
  while (f >= 1024 && i < 4) { f /= 1024; i++; } return f.toFixed(i ? 1 : 0) + " " + u[i];
}
export function esc(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
export function setText(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; }
/* What a person, a pet or a place is counted in.

   "Files", not "photos". The number behind it is a count of distinct FILES
   (services/people.py: `COUNT(DISTINCT fa.file_id)`), and a face is found in a
   video as readily as in a photograph -- so a group of eleven that includes two
   clips was calling itself "11 photos" and was wrong twice: about two of them,
   and about what the screen can hold.

   One function because four screens print this number -- a card, a card
   mid-rename, a group's own page, and the toast after a merge -- and a count
   that names itself differently on two of them reads as two different counts. */
export function fileCount(n) {
  return `${(n || 0).toLocaleString()} file${n === 1 ? "" : "s"}`;
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function fmtDate(v) {
  if (!v) return "-";
  const p = v.split("T")[0].split("-");
  if (p.length === 1) return p[0];                                  // year
  if (p.length === 2) return (MONTHS[(+p[1]) - 1] || p[1]) + " " + p[0];     // year-month
  return v.replace("T", " ");                                     // full day/datetime
}
// How long a job has been going, at the precision a person reading a status
// row actually wants. The server sends seconds to one decimal place, which is
// right for a log and wrong here: a dedup pass 21 minutes in reported itself as
// "1300.2s", a number nobody converts in their head and whose last digit
// changes ten times a second.
export function fmtElapsed(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  // Rounded to the minute below the hour, so a long run reads "2 h 05 min"
  // rather than drifting between "2 h" and "3 h" for half an hour.
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}
let _toastT = null;
export function toast(msg, isErr) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.className = "show" + (isErr ? " err" : "");
  clearTimeout(_toastT); _toastT = setTimeout(() => { t.className = isErr ? "err" : ""; }, 2800);
}
