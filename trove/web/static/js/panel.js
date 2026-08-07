// What one item's inspector says, as pure functions of the payload.
//
// Split from item.js, which owns the viewer itself -- opening, moving between
// files, the stage, the map and the edit flows. Everything here takes the item
// and returns markup, touches no module state and reads nothing from the DOM,
// which is what makes the two rules below checkable by reading one file.
//
// * **A feature this archive declined produces no section at all.** Not an
//   empty one, not an explanation. The setup panel is where an archive says
//   what it wants; the viewer must not re-open that conversation on every file.
// * **"Found nothing" and "not looked at yet" are different facts.** Every
//   section that can be empty branches on `it.read`, and says which of the two
//   it is. An archive mid-pipeline is mostly the second case, and rendering it
//   as the first claims a finding we do not have.

import {
  esc, fmtBytes, fmtDate,
} from "./dom.js";
import {
  boxesShown,
} from "./boxes.js";
import {
  S, TYPE_ICON, archiveHasFeature, typeLabel,
} from "./state.js";

const has = id => archiveHasFeature(S.arch, id);

/* The whole panel, in the order a file's story reads: who is in it, where it
   was taken, what it says, and then everything measurable about it.

   Details is the last section about THIS file, and it absorbs what used to be a
   File section of its own -- the path and the two ways of opening it are facts
   of the same kind as its dimensions and its date, and a heading over two links
   was a section in name only.

   The two sections about OTHER files come after all of it, most literal first:
   the copies of this exact file, then the ones that merely look like it.
   "Looks like this" is last because it is also the only section that starts
   work when you press it, so it cannot push the readings down the panel as it
   fills with results. */
export function renderPanel(it, related) {
  return `<h3>${esc(it.name)}</h3>` +
    `<div class="subline">${esc(typeLabel(it.type))} \u00b7 ${fmtBytes(it.size)}</div>` +
    peopleSection(it) + petsSection(it) + placeSection(it) + textSection(it) +
    detailsSection(it) + duplicatesSection(it) + looksLikeSection(it, related);
}

function notYet(line, sub) {
  return `<div class="notyet"><span>${esc(line)}<span class="sub">${esc(sub)}</span></span></div>`;
}

/* The date as a row of Details rather than a section of its own. It is a fact
   about the file like its size, not the headline the panel opens with -- and
   the editor still lands in #dateval, so `editDate()` is unchanged.

   All three parts on one line: the date, where it came from, and the way to
   change it. They are one fact and its provenance, and the row used to break
   them across three places -- "Edit" up in the label column beside the word
   "Date", the date on the right, and the source dropping to a line of its own
   underneath, which made a two-line row out of six words.

   `.dateline` is what keeps the editor working: #dateval stays the container
   editDate() replaces wholesale, and only its contents sit in a row. */
function dateRow(it) {
  return `<div class="kv datekv"><span class="k">Date</span>
    <span class="v" id="dateval"><span class="dateline">${fmtDate(it.date)}${dateProv(it)}</span></span>
    <button class="iconbtn" type="button" onclick="editDate()" title="Edit date"
      aria-label="Edit date">${PENCIL}</button></div>`;
}
const PENCIL = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3Z"/><path d="M14.5 6.5 17 9"/></svg>`;
/* The date's source, in words. It was always resolved and always sent; the
   panel simply printed the raw column ("mtime") in grey and left the user to
   know what that meant. Next to an Edit button, how much the date can be
   trusted is the single most useful thing the section can say.

   `exif` is the timestamp a file carries about itself, whatever wrote it. For a
   photo that is the shutter; for a PDF it is whatever exiftool finds in
   CreateDate -- Word, a scanner, a bank's statement generator -- and the enrich
   stage files both under the one key (metadata/resolver.py). So the words are
   about the *file*, not the instrument: this said "From the camera" over every
   contract in the archive.

   Three words each, because the badge now shares a line with the date it
   qualifies. The sentence each one shortens is on the badge's tooltip, and the
   part that cannot be shortened -- how far the date can be trusted -- is the
   coloured dot, which says it without spending a word. */
const DATE_SOURCE = {
  exif: ["From metadata", "", "The timestamp the file carries about itself"],
  takeout_json: ["From Takeout", "", "From the Google Takeout sidecar"],
  filename: ["From filename", "guess", "Guessed from the file's own name"],
  mtime: ["From timestamp", "weak", "The file's modification time, which is often wrong"],
  manual: ["You set this", "", "Set by hand, and never overwritten"],
};
function dateProv(it) {
  if (!it.date) return "";
  const [words, tone, full] = DATE_SOURCE[it.date_source] || [it.date_source || "", "", ""];
  if (!words) return "";
  return `<span class="prov ${tone}" title="${esc(full || words)}">${esc(words)}</span>`;
}

/* Coordinates and places are not the same feature. `geo` is written by the
   enrich stage, which the required `index` feature owns, so a file carries its
   coordinates whatever else is off; the optional `places` feature owns only the
   clustering -- the name, the count, the ability to change it. With Places off
   the map stays and those three go, because cutting the whole section would
   hide a fact the archive genuinely has. */
function placeSection(it) {
  // Only something that was taken can have been taken somewhere. A spreadsheet
  // has no there: it was written, and wherever the laptop was that day is not a
  // fact about the file. So a document gets no map, no coordinates and no offer
  // to attach one -- the same rule People states in words a few sections down,
  // said here by leaving the section out, because an empty Place section would
  // be an invitation to answer a question the file cannot be asked.
  if (it.type !== "image" && it.type !== "video") return "";
  const clustered = has("places");
  if (!it.gps && !clustered) return "";
  if (!it.gps) {
    return `<div class="isec"><div class="h">Place <button class="linkbtn" onclick="editPlace()">Set</button></div>
      <div id="placeval" class="val">${it.place
        ? (it.place.name ? esc(it.place.name) : `<span class="imuted">Name this place</span>`)
        : `<span class="imuted">No coordinates on this file. You can still attach it to a place.</span>`}</div></div>`;
  }
  const name = clustered && it.place && it.place.name ? esc(it.place.name) : "From the camera";
  return `<div class="isec">
    <div class="h">${clustered ? "Place" : "Where it was taken"}
      ${clustered ? `<button class="linkbtn" onclick="editPlace()">Change</button>` : ""}</div>
    <div id="placeval" class="val">
      <div class="imap" id="imap"><div class="pinhold"><div class="pin"></div></div></div>
      <div class="imapcap"><span>${name}</span>
        <span class="coords">${it.gps.lat.toFixed(5)}, ${it.gps.lon.toFixed(5)}</span></div>
      <div class="itilenote">${it.gps.alt != null ? `Altitude ${Math.round(it.gps.alt)} m · ` : ""}${esc(gpsSourceWords(it.gps.source))}.
        Street tiles load once you stop here: coordinates only, your media stays on this computer.</div>
    </div></div>`;
}
function gpsSourceWords(src) {
  return src === "takeout_json" ? "from the Google Takeout sidecar" : "from the camera";
}

function peopleSection(it) {
  if (!has("people")) return "";
  const rows = (it.people || []).map(f => faceRow(it, f)).join("") +
    (it.manual_people || []).map(manualPersonRow).join("");
  let body;
  if (rows) body = `<div class="facelist">${rows}</div>`;
  else if (it.type !== "image" && it.type !== "video")
    body = `<div class="imuted">Faces are read from photos and videos.</div>`;
  else if (!it.read.people)
    body = notYet("Faces not read yet", "This file is still in the queue.");
  else body = `<div class="imuted">No faces detected.</div>`;
  // Showing where the faces are is a fact about THIS section, so the control
  // for it lives here rather than in the chrome over the photo.
  const boxable = it.type === "image" && (it.people || []).some(f => f.box);
  const toggle = boxable
    ? `<button class="linkbtn" onclick="toggleBoxes()" id="boxtoggle">${boxesShown() ? "Hide on photo" : "Show on photo"}</button>`
    : "";
  return `<div class="isec"><div class="h">People ${toggle}</div>
    ${body}${tagPicker(it, "person")}</div>`;
}

/* The picker itself, always on when it can do anything.

   It used to hide behind an "Add" button, which bought nothing: choosing a name
   saves it immediately (onAddPerson posts on change), so the button was one
   click in front of a control that already says what it is. Drawn only when
   there is somebody left to choose -- an archive with nobody named, or a file
   where everyone named is already tagged, gets no control rather than one that
   explains why it cannot help. */
function tagPicker(it, kind) {
  const people = kind === "person";
  const named = (people ? it.person_options : it.pet_options) || [];
  if (!named.length) return "";
  const taken = new Set(
    people
      ? [...(it.people || []).filter(f => f.person_id).map(f => f.person_id),
        ...(it.manual_people || []).map(p => p.person_id)]
      : [...(it.animals || []).filter(a => a.pet_id).map(a => a.pet_id),
        ...(it.manual_pets || []).map(p => p.pet_id)]
  );
  const free = named.filter(o => !taken.has(o.id));
  if (!free.length) return "";
  const options = free.map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  // The two handlers are written out rather than picked into a variable: the
  // build check that proves an inline `on*` names something main.js exports
  // reads these attributes as text, and cannot see through `onchange="${x}"`.
  return people
    ? `<select class="fsel tagpick" onchange="onAddPerson(this.value)">
        <option value="" selected>Tag someone…</option>${options}</select>`
    : `<select class="fsel tagpick" onchange="onAddPet(this.value)">
        <option value="" selected>Tag a pet…</option>${options}</select>`;
}

function petsSection(it) {
  if (!has("pets")) return "";
  const rows = (it.animals || []).map(a => `<div class="facerow">
      <img class="facecrop" src="/animalThumb/${a.detection_id}" loading="lazy">
      <span>${a.name ? `<strong>${esc(a.name)}</strong> ` : ""}<span class="pet-species">${esc(a.species)}</span>
      <span class="imuted">${Math.round(a.score * 100)}%</span></span></div>`).join("") +
    (it.manual_pets || []).map(manualPetRow).join("");
  let body;
  if (rows) body = `<div class="facelist">${rows}</div>`;
  else if (it.type !== "image" && it.type !== "video")
    body = `<div class="imuted">Pets are read from photos and videos.</div>`;
  else if (!it.read.pets)
    body = notYet("Pets not read yet", "This file is still in the queue.");
  else body = `<div class="imuted">No pets detected.</div>`;
  return `<div class="isec"><div class="h">Pets</div>
    ${body}${tagPicker(it, "pet")}</div>`;
}

/* Two shapes, each behind its own feature. A picture's writing is short and the
   point is what it says, so the whole transcript is here. A document's words
   are thousands and the document itself is on the stage, so it reports only
   that it was read, by which reader, and how much. */
function textSection(it) {
  const t = it.text;
  const isPicture = it.type === "image";
  const feature = isPicture ? "ocr" : "documents";
  if (!has(feature)) return "";
  // Only file kinds a reader could ever open get a section at all.
  if (!isPicture && it.type !== "document") return "";
  if (!it.read.text) {
    return `<div class="isec"><div class="h">Detected text</div>
      ${notYet("Not read yet", isPicture
        ? "Reading the writing in this picture is still queued."
        : "Reading this document is still queued.")}</div>`;
  }
  if (!t) {
    return `<div class="isec"><div class="h">Detected text</div>
      <div class="imuted">No text detected.</div></div>`;
  }
  if (t.transcript) {
    return `<div class="isec"><div class="h">Detected text
        <button class="linkbtn" onclick="copyText()">Copy</button></div>
      <div class="textcard"><div class="body" tabindex="0">${esc(t.transcript)}</div>
        <div class="foot"><span>${wordish(t.chars)}${t.confidence != null
          ? ` · confidence ${t.confidence.toFixed(2)}` : ""}</span><span>Read by OCR</span></div></div></div>`;
  }
  return `<div class="isec"><div class="h">Detected text</div>
    <div class="readflag">
      <span class="mark"><svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/></svg></span>
      <span><span class="lead">${esc(readerWords(t))}</span>
        <span class="meta">${wordish(t.chars)}${t.pages ? ` · ${t.pages} page${t.pages === 1 ? "" : "s"}` : ""}</span></span>
    </div></div>`;
}
function readerWords(t) {
  return t.extractor === "pdf-ocr" ? "Read by OCR from scanned pages"
    : t.extractor === "pdf-text" ? "Read from the PDF's own text layer"
      : t.extractor === "office" ? "Read with the Word reader"
        : t.extractor === "opendocument" ? "Read with the OpenDocument reader"
          : "Read from the file's text";
}
// Characters are what the row stores; words are what a person estimates length
// in. Five-ish characters per word is close enough for "how long is this".
export function wordish(chars) {
  const w = Math.max(1, Math.round((chars || 0) / 5.5));
  return `${w.toLocaleString()} word${w === 1 ? "" : "s"}`;
}

/* Which folder, not just how many others are in it: "12 other files" says
   nothing you can act on, and the folder's own name is what places the file in
   the collection. */
function folderCell(it) {
  const where = it.folder ? esc(it.folder) : "the archive root";
  if (!it.folder_count) return `${where} · only this file`;
  return `${where} · ${it.folder_count.toLocaleString()} other file${it.folder_count === 1 ? "" : "s"}`;
}
/* The copies of this exact file, as the copies themselves.

   It used to be one row of Details reading "3 copies", which is the one thing
   the panel is in a position to improve on: it says three files somewhere are
   the same and leaves you to open the Duplicates screen and hunt for them. The
   group is small and already grouped, so it is shown -- each copy with what
   makes it a copy, which of them Trove keeps, and where you are among them.

   The vocabulary is the Duplicates screen's, deliberately: kept, identical
   copy, visual match. Two screens naming the same fact differently is how a
   user ends up believing they are two facts. */
function duplicatesSection(it) {
  const d = it.duplicates;
  const body = d ? dupGroup(it, d)
    : !it.read.duplicates
      ? notYet("Not compared yet", "Trove has not looked for copies of this file.")
      : `<div class="imuted">No duplicates found.</div>`;
  return `<div class="isec"><div class="h">Duplicates</div>${body}</div>`;
}
function dupGroup(it, d) {
  const others = Math.max(0, d.count - 1);
  const copies = `${others} other cop${others === 1 ? "y" : "ies"}`;
  // What the group means for the file in front of you, which is the one thing
  // the Duplicates screen cannot say: it lists groups, not the file you opened.
  const lead = d.canonical
    ? `${copies}. This is the one Trove shows.`
    : `${copies}. This one is hidden from browsing; the copy marked kept is shown instead.`;
  return `<div class="copies">${d.members.map(m => copyTile(m, it.id)).join("")}</div>
    <div class="imuted">${esc(lead)}</div>`;
}
const COPY_TAG = { canonical: "✓ Kept", identical: "Identical", visual: "Looks the same" };
/* One copy. The file you are looking at is marked and does not open itself;
   every other copy is a way into that copy, with the arrows then walking the
   group -- the same claim about "next" the Duplicates screen makes. */
function copyTile(m, openId) {
  const here = m.id === openId;
  const tag = `<span class="ctag ${m.match_type}">${here ? "This file" : COPY_TAG[m.match_type]}</span>`;
  const face = m.type === "image" || m.type === "video" || m.type === "document"
    ? `<img src="/thumb/${m.id}" loading="lazy" alt=""
        onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'cph',textContent:'${TYPE_ICON[m.type] || "📦"}'}))">`
    : `<span class="cph">${TYPE_ICON[m.type] || "📦"}</span>`;
  const where = `${esc(m.folder || "the archive root")} · ${esc(m.name)}`;
  const cls = `copy ${m.match_type}${here ? " here" : ""}`;
  if (here) return `<div class="${cls}" title="${where}" aria-current="true">${face}${tag}</div>`;
  return `<button type="button" class="${cls}" title="${where}"
    onclick="openCopy(${m.id})">${face}${tag}</button>`;
}

/* Its own section, with its own name.

   This used to sit under a "Related" heading directly beneath the folder count,
   which made a button labelled "Show related files" read as "show me the rest of
   this folder" -- the wrong promise for a control that searches the whole
   archive by what a picture looks like. Named for what it actually answers, and
   kept apart from the folder facts, which are now plain rows in Details. */
function looksLikeSection(it, related) {
  if (!has("semantic")) return "";
  if (it.type !== "image" && it.type !== "video") return "";
  if (!it.read.semantic) {
    return `<div class="isec"><div class="h">Looks like this</div>
      ${notYet("Not described for search yet", "This can be answered once the file is indexed.")}</div>`;
  }
  const body = related
    ? relStrip(related)
    : `<button class="findbtn" onclick="showRelated()">Find similar pictures</button>`;
  return `<div class="isec"><div class="h">Looks like this</div>
    <div id="relhold">${body}</div></div>`;
}
export function relStrip(items) {
  if (!items.length) return `<div class="imuted">Nothing else in the archive looks like this one.</div>`;
  return `<div class="relstrip">${items.map(r =>
    `<button type="button" onclick="openRelated(${r.id})" title="${esc(r.name || "")}">
      <img src="/thumb/${r.id}" loading="lazy" alt=""></button>`).join("")}</div>`;
}

function detailsSection(it) {
  const kv = (k, v, cls) => v != null && v !== ""
    ? `<div class="kv"><span class="k">${k}</span><span class="v${cls ? " " + cls : ""}">${v}</span></div>` : "";
  const dims = it.meta && it.meta.width ? `${it.meta.width}×${it.meta.height}` : "";
  const cam = it.meta && it.meta.model ? ((it.meta.make || "") + " " + it.meta.model).trim() : "";
  const dur = it.meta && it.meta.duration_s ? fmtDuration(it.meta.duration_s) : "";
  // No Size row: it is already in the subline under the file's name, and the
  // same number twice in one panel reads as two different numbers at a glance.
  return `<div class="isec"><div class="h">Details</div>` +
    dateRow(it) + kv("Dimensions", dims) + kv("Length", dur) + kv("Camera", cam) +
    kv("Folder", folderCell(it)) +
    kv("Description", it.description ? esc(it.description) : "") +
    takeoutRows(it) + fileRows(it) + `</div>`;
}
/* Where the file is and the two ways to open it, as the tail of Details rather
   than a section of its own: a heading over two links is a heading over
   nothing, and a path is a detail like any other. */
function fileRows(it) {
  return `<div class="kv"><span class="k">File</span>
      <span class="v filepath">${esc(it.rel_path)}</span></div>
    <div class="filelinks">
      <a href="/file/${it.id}" target="_blank">Open original ↗</a>
      ${window.archiveDesktop ? `<button class="linkbtn" onclick="openFileLocation()">Open file location ↗</button>` : ""}
    </div>`;
}
function takeoutRows(it) {
  const t = it.takeout;
  if (!t || !t.match_method) return "";
  const conf = t.match_confidence != null ? ` · ${Math.round(t.match_confidence * 100)}% sure` : "";
  return `<div class="kv"><span class="k">From Takeout</span><span class="v">matched by ${esc(t.match_method)}${conf}</span></div>`;
}
function fmtDuration(s) {
  const t = Math.round(s);
  if (t < 60) return `${t} s`;
  return `${Math.floor(t / 60)} min ${String(t % 60).padStart(2, "0")} s`;
}

/* One detected face, with the select that reassigns it, and the two manual-tag
   rows for media where nothing was detected at all. Markup only: what the
   controls DO lives with the optimistic-save flows in item.js. */
function faceRow(it, f) {
  const named = it.person_options || [];
  const isNamed = f.person_id && f.name;
  let opts = isNamed ? "" : `<option value="" selected>${f.name ? esc(f.name) : "unknown"}</option>`;
  named.forEach(p => { opts += `<option value="${p.id}"${p.id === f.person_id ? " selected" : ""}>${esc(p.name)}</option>`; });
  if (!named.length && !isNamed)
    return `<div class="facerow"><img class="facecrop" src="/faceThumb/${f.face_id}" loading="lazy" onerror="this.style.visibility='hidden'">
      <span class="muted" style="font-size:12px">Name people in the People section to label them here.</span></div>`;
  // `data-face-id` is what lets hovering the row highlight that person's box
  // on the photo -- the only way to tell which box is whose in a group shot.
  return `<div class="facerow" data-face-id="${f.face_id}"
      onmouseenter="highlightFace(${f.face_id})" onmouseleave="highlightFace(null)">
    <img class="facecrop" src="/faceThumb/${f.face_id}" loading="lazy" onerror="this.style.visibility='hidden'">
    <select class="fsel" title="Reassign this face" onchange="reassignFace(${f.face_id},this.value,this)">${opts}</select></div>`;
}
function manualPersonRow(p) {
  return `<div class="facerow">
    <div class="facecrop placeholder">👤</div>
    <span style="flex:1;min-width:0"><strong>${esc(p.name)}</strong></span>
    <button class="linkbtn" onclick="removeManualPerson(${p.person_id})">Remove</button></div>`;
}
function manualPetRow(p) {
  return `<div class="facerow">
    <div class="facecrop placeholder">🐾</div>
    <span style="flex:1;min-width:0"><strong>${esc(p.name)}</strong></span>
    <button class="linkbtn" onclick="removeManualPet(${p.pet_id})">Remove</button></div>`;
}
