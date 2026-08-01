// The state every screen shares, and the constants that describe the app's
// shape. `S` is a plain object rather than a set of exported `let`s on purpose:
// an imported binding is read-only, so a module could read `S.arch` but never
// assign it. Mutating one shared object works from anywhere.

export const TYPE_ICON = { image: "🖼️", video: "🎞️", audio: "🎵", document: "📄", archive: "🗜️", other: "📦" };
export const TYPE_COL = { image: "#ff375f", video: "#ff9f0a", audio: "#30d158", document: "#64d2ff", archive: "#bf5af2", other: "#8e8e93" };
const TYPE_LABEL = { archive: "compressed" };
export const typeLabel = t => TYPE_LABEL[t] || t;
export const ICONS = {
  overview: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></svg>',
  library: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m4 17 5-5 3.5 3.5 2-2L20 19"/></svg>',
  timeline: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5M12 7v5l3 2"/></svg>',
  people: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c.5-3.3 2.3-5 5.5-5s5 1.7 5.5 5"/><circle cx="17.5" cy="9" r="2.5"/><path d="M15.5 15c2.8-.5 4.6.8 5 3.5"/></svg>',
  pets: '<svg viewBox="0 0 24 24"><path d="M8.5 10.5C6 7 3 7.5 3 11c0 2 1.5 3.5 3.5 3.5C5 18 7.5 21 12 21s7-3 5.5-6.5C19.5 14.5 21 13 21 11c0-3.5-3-4-5.5-.5"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/><path d="M10 17h4"/></svg>',
  places: '<svg viewBox="0 0 24 24"><path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></svg>',
  dups: '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="3"/><path d="M16 8V7a3 3 0 0 0-3-3H7a3 3 0 0 0-3 3v6a3 3 0 0 0 3 3h1"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M20.5 15.3A9 9 0 0 1 8.7 3.5 9 9 0 1 0 20.5 15.3Z"/></svg>'
};
export const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "library", label: "Library" },
  { id: "timeline", label: "Timeline" },
  { id: "people", label: "People" },
  { id: "pets", label: "Pets" },
  { id: "places", label: "Places" },
  { id: "dups", label: "Duplicates" },
];
export const S = {
  arch: null, section: "overview", grid: null,
  timeline: { bucket: "month", year: "", month: "", people: [], place: "" }, poll: null,
  // Bumped on every user navigation (section switch / archive open). Async renders
  // capture it and bail if it changed while they were awaiting, so a slow fetch can
  // never paint a stale section over the one the user just picked.
  nav: 0,
};
