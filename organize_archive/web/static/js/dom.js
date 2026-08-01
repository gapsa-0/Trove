// Small DOM and formatting helpers, shared by every screen. Nothing here reads
// application state or talks to the server; if a helper needs either, it belongs
// with the screen that owns it.

export function fmtBytes(n) {
  if (n == null) return "-"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0, f = n;
  while (f >= 1024 && i < 4) { f /= 1024; i++; } return f.toFixed(1) + " " + u[i];
}
export function esc(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
export function setText(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; }
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function fmtDate(v) {
  if (!v) return "-";
  const p = v.split("T")[0].split("-");
  if (p.length === 1) return p[0];                                  // year
  if (p.length === 2) return (MONTHS[(+p[1]) - 1] || p[1]) + " " + p[0];     // year-month
  return v.replace("T", " ");                                     // full day/datetime
}
let _toastT = null;
export function toast(msg, isErr) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.className = "show" + (isErr ? " err" : "");
  clearTimeout(_toastT); _toastT = setTimeout(() => { t.className = isErr ? "err" : ""; }, 2800);
}
