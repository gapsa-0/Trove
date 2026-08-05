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
  // Search by description unlocks no nav section of its own, so this mark is
  // only ever drawn on its setup card and its Overview card. It is the same
  // magnifier the setup card's preview types into.
  semantic: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4.5 4.5"/></svg>',
  // Searching file names. Not a feature -- every archive can do it and none
  // chose it -- but it is one of the three ways Browse can answer a query, so
  // it needs a mark on the same footing as the two that are. A luggage tag:
  // the name a thing was given, as opposed to anything inside it.
  filename: '<svg viewBox="0 0 24 24"><path d="M20.6 12.7 12.7 20.6a2 2 0 0 1-2.8 0l-6.5-6.5a2 2 0 0 1-.6-1.6l.5-6a2 2 0 0 1 1.8-1.8l6-.5a2 2 0 0 1 1.6.6l6.5 6.5a2 2 0 0 1 0 2.8Z"/><circle cx="8.5" cy="8.5" r="1.2"/></svg>',
  // Documents unlocks no nav section either: it widens what the one search box
  // reaches. A page with lines of writing on it, which is what the feature is
  // about -- the words inside the file rather than the file itself.
  documents: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/></svg>',
  // Search by meaning: the same page, with the magnifier that marks the other
  // search laid over it. The two marks are siblings because the two features
  // are: one finds the words, one finds what they are about.
  // Text in images: a picture frame with writing inside it, sibling to the
  // documents page mark -- the same words, found somewhere else.
  ocr: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 10h10M7 14h6"/><circle cx="17.5" cy="14.5" r="1"/></svg>',
  meaning: '<svg viewBox="0 0 24 24"><path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4"/><path d="M13 3v5h5V8Z"/><path d="M9 12h4"/><circle cx="16.5" cy="16.5" r="3.5"/><path d="m19.2 19.2 2.3 2.3"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M20.5 15.3A9 9 0 0 1 8.7 3.5 9 9 0 1 0 20.5 15.3Z"/></svg>'
};
// Every section the app can show, in nav order. Which of them an archive
// actually gets is decided by its feature set: the server sends the unlocked
// section ids with each archive (trove/features.py), and
// `archiveSections` filters this list against them. An archive from before
// features existed reports the full set, so nothing disappears on upgrade.
export const SECTIONS = [
  { id: "overview", label: "Overview" },
  // "Browse", not "Library": every other section is named for what it holds,
  // and this one is named for what you do there -- look through the whole
  // archive, by filter or by description. "Library" stays the word for the
  // collection itself ("Search your library", "Library health"), which is
  // exactly the thing this screen is one view of. The id is untouched: it is
  // in URL hashes, in the feature catalogue's `sections`, and in ICONS.
  { id: "library", label: "Browse" },
  { id: "timeline", label: "Timeline" },
  { id: "people", label: "People" },
  { id: "pets", label: "Pets" },
  { id: "places", label: "Places" },
  { id: "dups", label: "Duplicates" },
];
// Sections keyed by the feature that unlocks them. The Overview is unlisted
// because it reports the pipeline itself and is never gated.
const SECTION_FEATURE = {
  library: "index", timeline: "index", dups: "duplicates",
  people: "people", pets: "pets", places: "places",
};
export function archiveSections(archive) {
  const on = new Set(archive?.features || []);
  if (!on.size) return SECTIONS;
  return SECTIONS.filter(s => !SECTION_FEATURE[s.id] || on.has(SECTION_FEATURE[s.id]));
}
// Whether an archive runs one feature. For the features that unlock no section
// of their own and so cannot be gated by hiding one: Search by description
// lives *inside* Browse, as the composer at the top of it, and an archive that
// declined the feature must not be offered a search that will never have
// anything to find. Same "unconfigured means everything" rule as above.
export function archiveHasFeature(archive, id) {
  const on = new Set(archive?.features || []);
  return !on.size || on.has(id);
}
export const S = {
  arch: null, section: "overview", grid: null,
  // The text-results group, present only for an archive that reads its
  // documents. Null everywhere else, which is what activeGrids() reads.
  textGrid: null,
  timeline: { bucket: "month", year: "", month: "", people: [], place: "" }, poll: null,
  // Bumped on every user navigation (section switch / archive open). Async renders
  // capture it and bail if it changed while they were awaiting, so a slow fetch can
  // never paint a stale section over the one the user just picked.
  nav: 0,
};
