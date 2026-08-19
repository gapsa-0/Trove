// Every tile the app draws, and only that.
//
// Four kinds, because a result can arrive from four different places and the
// tile has to say which: a plain thumbnail, a passage of text a document
// matched with, a filename with the matching part marked, and a face with the
// control that says "not this person". Nine screens render one of these, and
// all of them used to import it from library.js -- which owns the grid, its
// paging and its filter bar, and had no business also owning how a single
// result looks.
//
// The split is by what a change would be about. A change to *which* files come
// back is next door; a change to what one of them looks like is here.

import {
  jpost,
} from "./api.js";
import {
  askConfirm,
} from "./merge.js";
import {
  esc, toast,
} from "./dom.js";
import {
  openItem,
} from "./item.js";
import {
  ICONS, MARKS, TYPE_ICON,
} from "./state.js";

/* One thumbnail. `caption` says what the strip along its bottom reads: the
   file's own name in Browse, where the grid is already broken into dated
   sections and repeating the date under every tile says nothing the heading
   above it did not -- and the date on the grids that have no such headings
   (a person's photos, a pet's, a place's), where it is the only thing placing
   the shot in time. */
export function tile(it, resultIndex = null, caption = "date") {
  const d = document.createElement("button"); d.type = "button"; d.className = "tile";
  d.dataset.name = (it.name || "").toLowerCase(); d.dataset.fileId = it.id;
  if (resultIndex != null) d.dataset.resultIndex = resultIndex;
  // The pip is decorative markup, so its meaning rides on the tile's own
  // label instead of adding a second stop per tile for screen readers.
  d.setAttribute("aria-label", (it.name || "Open media item") +
    (it.indexed ? ", indexed for description search" : "") +
    (it.has_gps ? ", has a location" : ""));
  d.onclick = () => openItem(it.id);
  d.appendChild(thumbNode(it));
  const cap = document.createElement("div"); cap.className = "cap";
  // A name is arbitrary user data and long enough to need cutting off, so it is
  // escaped, truncated by CSS, and given a title carrying the whole of it.
  const name = it.name || "";
  const label = caption === "name"
    ? `<span class="cap-label" title="${esc(name)}">${esc(name)}</span>`
    : `<span class="cap-label">${(it.date || "").slice(0, 10)}</span>`;
  // `indexed` is absent on description-search results -- every hit there is
  // indexed by definition, so the pip would mark all of them and say
  // nothing. Undefined simply renders no pip, which is the wanted result.
  cap.innerHTML = label + `<span class="cap-marks">` +
    (it.indexed ? `<span class="indexed" title="Indexed for description search"></span>` : "") +
    (it.type === "video" ? "<span>▶</span>" : "") + `</span>`;
  d.appendChild(cap);
  // aria-hidden because the meaning is already on the tile's own label:
  // left alone a screen reader announces the raw emoji ("pushpin"), which
  // is the glyph rather than what it tells you.
  if (it.has_gps) {
    const b = document.createElement("div");
    b.className = "badge"; b.innerHTML = MARKS.place;
    b.title = "Has a location"; b.setAttribute("aria-hidden", "true");
    d.appendChild(b);
  }
  return d;
}
// `innerHTML` because the stand-in is a drawn mark now, not a character.
function ph(icon) { const s = document.createElement("div"); s.className = "ph"; s.innerHTML = icon; return s; }
/* Swap a thumbnail that 404'd for its type's mark, from an inline `onerror`.

   A helper rather than the `this.replaceWith(Object.assign(...))` one-liner
   three of these used to carry: those built the stand-in by assigning
   `textContent`, which worked only while the stand-in was a single character.
   A drawn mark is markup, and markup carries double quotes, which is one thing
   an inline HTML attribute inside a JS template literal cannot hold. */
export function thumbFallback(img, type, cls = "ph") {
  const s = document.createElement("span");
  s.className = cls;
  s.innerHTML = TYPE_ICON[type] || TYPE_ICON.other;
  img.replaceWith(s);
}

/* The picture of a file, or the icon that stands in for one.

   Documents get a thumbnail too where one can be made -- a PDF renders its
   first page -- because what is printed on a page is the only thing that tells
   two contracts apart in a grid. The server answers 404 when it cannot render
   one, and `onerror` is already the path back to the type icon, so asking costs
   nothing on the formats that have none.

   Exported because the Duplicates screen lays its media out its own way and so
   kept its own copy of this rule -- a copy that left `document` out, which is
   how a duplicated contract showed a bare 📄 there and its first page on every
   other screen. The list of what can be drawn, and what stands in when it
   cannot, is one thing and now lives in one place. */
export function thumbNode(it) {
  if (!THUMBABLE.has(it.type)) return ph(TYPE_ICON[it.type] || TYPE_ICON.other);
  const img = document.createElement("img");
  img.loading = "lazy";
  // Decorative: every caller labels the control this sits inside, and a second
  // announcement of the same file name is noise on a screen reader.
  img.alt = "";
  img.src = "/thumb/" + it.id;
  // `shot` is what tells the caption there is a picture underneath it worth
  // shielding itself from; see `.tile.shot .cap` in grid.css. Set on load
  // rather than up front, because a thumbnail that 404s never arrives and the
  // stand-in that replaces it wants the plain caption.
  img.onload = () => img.closest(".tile")?.classList.add("shot");
  img.onerror = () => {
    img.closest(".tile")?.classList.remove("shot");
    img.replaceWith(ph(TYPE_ICON[it.type] || TYPE_ICON.image));
  };
  return img;
}
const THUMBABLE = new Set(["image", "video", "document"]);
/* A library tile plus the passage that matched -- for the text results group
   ONLY. tile() itself is shared with four other grids and must never grow this,
   so the snippet is attached afterwards, the way personTile attaches its own
   control.

   The snippet arrives with the match wrapped in two control characters rather
   than in markup. FTS5 does not escape the document text around the match, so
   returning `<mark>` from the server would mean a document containing the word
   "<script>" could put it into the page. Escaping first and substituting after
   is what keeps the highlight from being an injection point. */
export function textTile(it, mixedReaders = false) {
  const d = tile(it, null, "name");
  // Which reader produced the text, where that is not already answered by the
  // heading -- only worth saying when both are on and a hit could be either.
  // A file's own words and a best guess read off pixels are not the same claim.
  if (mixedReaders && it.reader) d.appendChild(foundBadge(READER_BADGE[it.reader]));
  if (!it.snippet) return d;
  const box = document.createElement("div");
  box.className = "tile-snippet";
  box.innerHTML = esc(it.snippet)
    .replaceAll("\u0002", "<mark>").replaceAll("\u0003", "</mark>");
  if (it.page != null) {
    const page = document.createElement("span");
    page.className = "tile-page";
    page.textContent = it.page_last && it.page_last !== it.page
      ? `pp. ${it.page}–${it.page_last}` : `p. ${it.page}`;
    box.prepend(page);
  }
  d.appendChild(box);
  return d;
}
// The words a badge carries, keyed by what the server reported. Named after the
// features the user chose from rather than after the extractor that ran: nobody
// switched on "pdf-ocr".
const READER_BADGE = {
  documents: { icon: "documents", text: "document text", hint: "Read from this file's own text" },
  ocr: { icon: "ocr", text: "text in pictures", hint: "Read from the writing in this picture" },
};
function foundBadge({ icon, text, hint }) {
  const b = document.createElement("span");
  b.className = "found-by";
  b.title = hint;
  const glyph = document.createElement("span");
  glyph.className = "ranking-mark";
  glyph.setAttribute("aria-hidden", "true");
  glyph.innerHTML = ICONS[icon];
  b.append(glyph, document.createTextNode(text));
  return b;
}
/* A library tile whose caption shows which words of the name matched.

   The one grid where that is worth drawing: a name search is a search *for* a
   name, so seeing which part of it answered is the whole result. Attached after
   tile() rather than folded into it, the way the snippet and the detach control
   are. */
export function nameTile(it, tokens) {
  const d = tile(it, null, "name");
  const label = d.querySelector(".cap-label");
  if (label && tokens.length) label.innerHTML = markedName(it.name || "", tokens);
  return d;
}
/* The words a name search required, lowercased -- the same split the server
   does (services/browse.py:_name_tokens), so what is marked is what matched. */
export function nameTokens(query) {
  return [...new Set((query || "").toLowerCase().split(/\s+/).filter(Boolean))];
}
/* The name with every matched run wrapped in a mark.

   Offsets are found on the lowercased name and applied to the original, and
   each slice is escaped as it is emitted -- so a file called `<b>.jpg` is
   marked up by this function and never by itself. */
function markedName(name, tokens) {
  const lower = name.toLowerCase();
  const spans = [];
  tokens.forEach(token => {
    for (let at = lower.indexOf(token); at >= 0; at = lower.indexOf(token, at + 1))
      spans.push([at, at + token.length]);
  });
  if (!spans.length) return esc(name);
  spans.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  let out = "", cursor = 0;
  spans.forEach(([start, end]) => {
    if (end <= cursor) return;            // wholly inside a run already marked
    start = Math.max(start, cursor);      // overlapping runs merge rather than nest
    out += esc(name.slice(cursor, start)) + "<mark>" + esc(name.slice(start, end)) + "</mark>";
    cursor = end;
  });
  return out + esc(name.slice(cursor));
}
// A library tile plus a "not this person" control, for the person detail
// page ONLY -- tile() itself is shared with the plain library grid, which
// must never grow this button. Detach removes the tile optimistically
// (mirrors reassignFace's discipline: mutate/repaint first, roll back +
// toast only if the POST actually fails).
export function personTile(it, personId) {
  return clusterTile(it, {
    crop: it.face_id,
    detachLabel: "This is not the person",
    confirm: "Remove this photo from this person? It won’t be suggested for them again.",
    setCover: () => jpost("/api/faces/person/cover",
      { person_id: personId, face_id: it.face_id }),
    coverThumb: `/faceThumb/${it.face_id}`,
    detach: () => jpost("/api/faces/detach", { person_id: personId, file_id: it.id }),
  });
}
/* The same tile for a pet's page. A pet's photo is identified by its detection
   rather than by a face, and the endpoints differ; nothing else does. */
export function petTile(it, petId) {
  return clusterTile(it, {
    crop: it.detection_id,
    detachLabel: "This is not the pet",
    confirm: "Remove this photo from this pet? It won’t be suggested for them again.",
    setCover: () => jpost("/api/pet/cover", { pet_id: petId, detection_id: it.detection_id }),
    coverThumb: `/animalThumb/${it.detection_id}`,
    detach: () => jpost("/api/pet/detach", { pet_id: petId, file_id: it.id }),
  });
}
/* A library tile plus the two things you can say about one photo of a group:
   that it represents them, and that it isn't them.

   Two buttons rather than the overflow menu the cards use, for two reasons
   that both come from the tile being a <button>: a <details> inside one is
   invalid HTML and does not reliably open, and the tile clips its own overflow
   to round the thumbnail, which would cut the panel off. */
function clusterTile(it, opts) {
  const d = tile(it);
  // No crop means a photo tagged by hand, with no detection in it to stand for
  // the group -- there is nothing for a cover to show.
  if (opts.crop) {
    d.appendChild(tileAction("tile-cover", "Make cover photo",
      "<svg viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='9' r='3.4'/><path d='M5 20c.7-4 3.4-6 7-6s6.3 2 7 6'/></svg>",
      () => setCover(opts)));
  }
  d.appendChild(tileAction("tile-detach", opts.detachLabel, '<svg class="appicon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>', () => detachTile(opts, d)));
  return d;
}
function tileAction(cls, label, glyph, onPick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `tile-action ${cls}`;
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.innerHTML = glyph;
  btn.onclick = e => { e.stopPropagation(); onPick(); };
  return btn;
}
function setCover(opts) {
  opts.setCover()
    .then(r => {
      if (!(r && r.ok)) {
        toast((r && r.error) ? "Couldn’t set the cover: " + r.error : "Couldn’t set the cover.", true);
        return;
      }
      toast("Cover photo set.");
      // The portrait in the top bar is the cover, so it is the one thing on
      // this page the change is visible in.
      const avatar = document.querySelector(".person-header-avatar");
      if (avatar && avatar.tagName === "IMG") avatar.src = `${opts.coverThumb}?t=${Date.now()}`;
    })
    .catch(() => toast("Couldn’t set the cover: connection error", true));
}
/* Optimistic: the tile goes first and comes back if the request fails, which
   is reassignFace's discipline -- repaint, then roll back and say so. */
async function detachTile(opts, tileEl) {
  if (!await askConfirm({
    title: opts.detachLabel, body: opts.confirm, confirmLabel: opts.detachLabel, danger: true,
  })) return;
  const parent = tileEl.parentNode, next = tileEl.nextSibling;
  tileEl.remove();
  opts.detach()
    .then(r => {
      if (!(r && r.ok)) {
        if (parent) parent.insertBefore(tileEl, next);   // roll back
        toast((r && r.error) ? "Couldn’t detach: " + r.error : "Couldn’t detach that photo.", true);
      } else {
        toast("Removed.");
      }
    })
    .catch(() => {
      if (parent) parent.insertBefore(tileEl, next);
      toast("Couldn’t detach: connection error", true);
    });
}
