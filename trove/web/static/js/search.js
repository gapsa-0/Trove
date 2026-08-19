// Library search: the contenteditable composer that turns typed names into
// person chips, the local English translation that runs before a description
// search, and the panel that says which ways this archive can be searched and
// how much of it each one currently reaches.

import {
  checkedPeople, liveRankings, mediaRanksQueries, reloadGrids, renderSortOptions,
  scrollResultsToTop,
  updateClearBtn, updatePeopleFilterLabel,
} from "./library.js";
import {
  renderGroupLabels,
} from "./results.js";
import {
  jget,
} from "./api.js";
import {
  esc,
} from "./dom.js";
import {
  ICONS, S, archiveHasFeature, typeLabel,
} from "./state.js";

let LOCAL_TRANSLATOR_PROMISE = null, SEARCH_SUBMISSION = 0;
function clearlyEnglishSearch(text) {
  // Short-query language detection is unreliable, but these structural words
  // are strong English signals and prevent feeding an already-English phrase
  // such as "besides a lake" through the Spanish translator. Ambiguous words
  // shared with Spanish ("a", "no", "me") are deliberately excluded.
  const signals = new Set(["the", "this", "that", "these", "those", "is", "are", "was", "were",
    "with", "without", "beside", "besides", "near", "by", "at", "of", "and", "or", "from", "to",
    "in", "on", "under", "over", "between", "inside", "outside", "during"]);
  return normalizedWords(text).split(" ").some(word => signals.has(word));
}
// Translate a Spanish query to English before embedding it. SigLIP 2's text
// tower is genuinely multilingual, so this looks redundant — it is not, and
// measurably so. A Spanish query gets hijacked by Spanish text *rendered
// inside* images, which this archive is full of (WhatsApp screenshots, memes,
// posters), because the model reads them. Measured over 30 query pairs on
// 2,000 real files: a Spanish query's top 10 is 57% screenshots against a
// 34% baseline, the English translation's is 30%. "un perro" returns ten dog
// memes; "a dog" returns ten photographs of dogs.
//
// And the wrong results score HIGHER (Spanish beats English on 22 of 30
// queries), which is why the translation must *replace* the original rather
// than be merged with it as an alternate vector — taking the best of both
// would systematically pick the worse one.
// Whether the translator's model files are on this machine at all, asked once.
//
// They stopped shipping inside the app and are fetched with Search by
// description instead (ADR 0019), so "not here" is now an ordinary state rather
// than a broken install: any archive that never switched that feature on has no
// translator and never will. Asking first is what keeps that cheap. Left to
// discover it by failing, the loader spends its whole `downloadTimeout` on a
// worker whose models 404 -- which put a fifteen-second stall in front of every
// search on exactly the archives that had least reason to pay it.
//
// A one-byte ranged GET rather than HEAD, because the /vendor route serves
// files through a Range-aware reader and answers GET only.
let TRANSLATOR_PRESENT = null;
function translatorPresent() {
  if (!TRANSLATOR_PRESENT) {
    TRANSLATOR_PRESENT = fetch("/vendor/translate-es-en-model.bin", {
      headers: { Range: "bytes=0-0" },
    }).then(r => r.ok).catch(() => false);
  }
  return TRANSLATOR_PRESENT;
}
async function localTranslator() {
  // The WASM runtime and the es-en model are ~23 MB between them, so whoever
  // asks first waits for the download and the worker spin-up. Building the
  // promise is separated from using it precisely so that cost can be paid
  // ahead of time -- see warmLocalTranslator.
  if (!(await translatorPresent())) return null;
  if (!LOCAL_TRANSLATOR_PROMISE) {
    LOCAL_TRANSLATOR_PROMISE = import("/vendor/bergamot-translator.js").then(module =>
      new module.LatencyOptimisedTranslator({
        pivotLanguage: null,
        registryUrl: "/vendor/translation-es-en.json",
        cacheSize: 256,
        downloadTimeout: 15000
      })
    );
  }
  return LOCAL_TRANSLATOR_PROMISE;
}
// Start that load when Library opens, so the first search is not the thing
// that waits for it. Deferred to idle rather than started inline: the grid is
// fetching its first page at the same moment, and 23 MB of WASM competing with
// that trades a slower screen for a faster search nobody has asked for yet.
// Fire-and-forget — a failure is swallowed and reset, leaving the first real
// translation to hit the same path and report properly.
export function warmLocalTranslator() {
  const start = () => {
    try {
      localTranslator().catch(() => { LOCAL_TRANSLATOR_PROMISE = null; });
    } catch { LOCAL_TRANSLATOR_PROMISE = null; }
  };
  if (typeof requestIdleCallback === "function") requestIdleCallback(start, { timeout: 3000 });
  else setTimeout(start, 1000);
}
async function localEnglishTranslation(text) {
  if (!text || !text.match(/\p{L}/u) || clearlyEnglishSearch(text)) return "";
  try {
    const translator = await localTranslator();
    // No translator on this machine: the query goes to the server as typed,
    // which is the same outcome as a translation that changed nothing.
    if (!translator) return "";
    const response = await translator.translate({ from: "es", to: "en", text, html: false });
    const translated = (response && response.target && response.target.text || "")
      .replace(/\s+/g, " ").trim().toLocaleLowerCase();
    return normalizedWords(translated) === normalizedWords(text) ? "" : translated;
  } catch (error) {
    // Translation improves recall but is never required for search. Reset a
    // failed loader so a transient worker/model error can recover next time.
    console.warn("Local Spanish search expansion unavailable:", error);
    LOCAL_TRANSLATOR_PROMISE = null;
    return "";
  }
}
// A translated query used to get " photo" appended before embedding, to nudge
// a terse phrase ("in the lake") toward actual photographs. That was the
// modality gap being corrected by hand, and it is now corrected properly:
// scoring subtracts each modality's own mean (semantic_search's `center`), so
// the generic photo-ness the cue added is exactly what gets removed again.
// Measured over 12 queries afterwards it cost 0.057 of score and helped none
// of them -- "the mountains" fell from 11 results to 4 -- so the two
// corrections were stacking. It also made "el bosque" and "the forest" behave
// differently, since only the translated one carried the suffix.
/* "Where Trove looks when you type": the panel under the box while nothing has
   been typed.

   It is the same object as the result headings, in its other state. That is the
   point of it rather than a saving: the screen has to say what can be searched
   *and* label what came back, and when those were two separate pieces of copy
   the first one drifted -- Browse spent three features describing itself as
   "by filter or by description". Here a ranking that gains a reader gains a row,
   and that row is both the promise and the label.

   It answers two questions per row. What this way matches, in words; and how
   much of the archive it can currently see, which is a number only the server
   has. The rows are drawn immediately and the counts filled in when they land,
   so the panel is never the thing holding up the screen. */
let SEARCH_WAYS_GEN = 0;
export function renderSearchWays() { searchWaysTick(++SEARCH_WAYS_GEN); }

// What each way's coverage line says, given the status payloads. Kept as one
// function per ranking so the sentence and the numbers it reads sit together.

// The kinds of file an index holds, counted. Both status endpoints answer with
// the same per-type tally, and the question the panel is asking is the same one
// for both -- what can this way actually see -- so the line is built once.
//
// It replaces a single total and a word for what had been done to it: "12,040
// read", "8,900 photos and videos indexed". The total was the least useful part
// of it. What tells you whether a way can answer your question is *what* it
// holds, and "read" and "indexed" are the stage's vocabulary rather than
// anything the reader needs.
const KINDS = { image: "images", video: "videos", document: "documents", audio: "audio files" };

function reach(list) {
  return (list || [])
    .filter(t => t.count)
    .map(t => `${t.count.toLocaleString()} `
      + (t.count === 1 ? typeLabel(t.type) : (KINDS[t.type] || typeLabel(t.type) + "s")))
    .join(" · ");
}

function nameCoverage() {
  const total = S.grid && S.grid.query ? null : (S.grid && S.grid.total);
  // Every file, with no qualifier: this way needs no index and no feature, so
  // there is nothing here that some of them could be short of.
  return total == null ? "" : `${total.toLocaleString()} files`;
}
/* How many files there are is the one coverage figure that comes from the grid
   rather than from a status endpoint, and the grid's first page can land either
   side of the panel being drawn. So the panel fills it in whenever the number
   changes instead of only when it is built -- otherwise whichever of the two
   arrived second decided whether the row said anything at all. */
export function updateWaysCoverage() {
  const cell = document.getElementById("way-cov-name");
  if (cell) cell.textContent = nameCoverage();
}
function textCoverage(s) {
  if (!s) return "";
  if (!s.configured) return "Not available in this installation";
  const read = s.read || 0, pending = s.pending || 0;
  // Which files were read is said in the row's own sentence, from the features
  // that are on; this is the count, and only the count. Passages are gone from
  // it: how many pieces a file was cut into for the index is a fact about the
  // index, and there is nothing anybody can do with it.
  if (!read) return pending
    ? `Nothing read yet · ${pending.toLocaleString()} queued`
    : "Nothing to read in this archive yet";
  return reach(s.by_type) + (pending ? ` · ${pending.toLocaleString()} queued` : "");
}
function photoCoverage(s) {
  if (!s) return "";
  if (!s.configured) return "Not available in this installation";
  const indexed = (s.by_type || []).reduce((n, t) => n + (t.count || 0), 0);
  const pending = s.pending || 0;
  if (!indexed) return pending
    ? `Nothing indexed yet · ${pending.toLocaleString()} queued`
    : "Nothing indexed yet";
  return reach(s.by_type) + (pending ? ` · ${pending.toLocaleString()} queued` : "");
}

async function searchWaysTick(gen) {
  if (gen !== SEARCH_WAYS_GEN || S.section !== "library" || !S.arch) return;
  const panel = document.getElementById("search-ways"); if (!panel) return;
  const ways = liveRankings();
  // Only while browsing: once a search runs, the result headings are this same
  // list saying what each way actually found.
  if (S.grid && S.grid.query) { panel.hidden = true; return; }
  const wants = kind => ways.some(w => w.id === kind);
  const [text, photo] = await Promise.all([
    wants("text") ? jget("/api/browse/text/status?root=" + S.arch.id).catch(() => null) : null,
    wants("media") ? jget("/api/browse/semantic/status?root=" + S.arch.id).catch(() => null) : null,
  ]);
  if (gen !== SEARCH_WAYS_GEN || S.section !== "library") return;
  const cur = document.getElementById("search-ways"); if (!cur) return;
  if (S.grid && S.grid.query) { cur.hidden = true; return; }
  const coverage = { name: nameCoverage(), text: textCoverage(text), media: photoCoverage(photo) };
  cur.hidden = false;
  cur.innerHTML =
    `<h3 class="ways-head">Where Trove looks when you type
       <span class="muted">${ways.length === 1 ? "one way" : ways.length + " ways"} in this archive</span>
     </h3>
     <div class="ways-list">${ways.map(w => `
       <div class="way">
         <span class="ranking-mark" aria-hidden="true">${ICONS[w.icon]}</span>
         <div class="way-text">
           <b>${esc(w.label)}${w.always ? `<span class="way-always">always</span>` : ""}</b>
           <span>${esc(w.matches)}</span>
         </div>
         <span class="way-links">${w.readers.map(readerLink).join("")}</span>
         <span class="way-cov" id="way-cov-${w.id}">${esc(coverage[w.id] || "")}</span>
       </div>`).join("")}</div>`;
  // Indexing runs on its own; keep the counts live until both drain.
  const busy = (text && text.pending) || (photo && photo.pending);
  if (busy) setTimeout(() => searchWaysTick(gen), 2500);
}
/* A way's link to what documents it, one per feature feeding it.

   The mark rather than the word, because the text way has two of them and a row
   of "How Search by document text works · How Search by picture text works" is
   several times longer than everything else on the row put together. The marks are already the vocabulary this
   screen labels results with, so making them the way in costs no new furniture
   -- and the name each one carries is on its tooltip and its accessible label,
   where a reader who needs the words gets them. */
function readerLink(reader) {
  if (!reader.docs) return "";
  const how = `How ${reader.label} works`;
  return `<button type="button" class="way-doc" onclick="openDocs('${esc(reader.docs)}')"
      title="${esc(how)}" aria-label="${esc(how)}">${ICONS[reader.icon]}</button>`;
}
function normalizedWords(value) {
  return (value || "").normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase()
    .replace(/[’']/g, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}
function editDistance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let diagonal = row[0]; row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const above = row[j], cost = a[i - 1] === b[j - 1] ? 0 : 1;
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, diagonal + cost); diagonal = above;
    }
  }
  return row[b.length];
}
function personWordMatches(queryWord, nameWord) {
  if (queryWord === nameWord) return { matched: true, exact: true };
  // Only the typed word may be a prefix of the name. This lets "mari " match
  // María but prevents a longer unrelated word such as "marinero" from doing so.
  if (queryWord.length >= 4 && nameWord.startsWith(queryWord)) return { matched: true, exact: false };
  if (queryWord.length >= 5 && Math.abs(nameWord.length - queryWord.length) <= 1 &&
    editDistance(nameWord, queryWord) <= 1) return { matched: true, exact: false };
  return { matched: false, exact: false };
}
function extractPeopleMentions(query, people, commitEnd = false) {
  const wordPattern = /[\p{L}\p{M}]+(?:[’'][\p{L}\p{M}]+)*/gu;
  const words = [...query.matchAll(wordPattern)].map(match => {
    const source = match[0], withoutPossessive = source.replace(/[’']s$/iu, "");
    return {
      start: match.index, end: match.index + withoutPossessive.length, source: withoutPossessive,
      norm: normalizedWords(withoutPossessive)
    };
  });
  const candidates = [];
  (people || []).forEach(person => {
    const nameWords = normalizedWords(person.name).split(" ").filter(Boolean);
    if (!nameWords.length) return;
    for (let i = 0; i + nameWords.length <= words.length; i++) {
      let exact = 0, matched = true;
      for (let j = 0; j < nameWords.length; j++) {
        const result = personWordMatches(words[i + j].norm, nameWords[j]);
        if (!result.matched) { matched = false; break; }
        if (result.exact) exact++;
      }
      if (matched) candidates.push({
        person, start: words[i].start, end: words[i + nameWords.length - 1].end,
        source: query.slice(words[i].start, words[i + nameWords.length - 1].end),
        wordCount: nameWords.length, exact
      });
    }
  });
  // Prefer the longest and most exact name at each position. Equally-good
  // ambiguous prefixes are left as text instead of silently choosing a person.
  const mentions = []; let usedUntil = -1;
  [...new Set(candidates.map(candidate => candidate.start))].sort((a, b) => a - b).forEach(start => {
    if (start < usedUntil) return;
    const here = candidates.filter(candidate => candidate.start === start)
      .sort((a, b) => b.wordCount - a.wordCount || b.exact - a.exact);
    if (!here.length) return;
    const best = here[0], tied = here.filter(candidate =>
      candidate.wordCount === best.wordCount && candidate.exact === best.exact);
    if (tied.length > 1) return;
    const next = query.slice(best.end, best.end + 1);
    best.committed = commitEnd ||
      best.end < query.length && !!next.match(/[^\p{L}\p{M}]/u) ||
      best.end === query.length && best.exact === best.wordCount;
    mentions.push(best); usedUntil = best.end;
  });
  return mentions;
}
/* The typed text with the recognised names lifted out of it, tidied back into a
   sentence -- the gap a removed name leaves behind is what the last three
   replacements close up.

   The punctuation rule only closes a gap in front of punctuation that ENDS a
   word ("Ada , and the lake" -> "the lake,"). It used to close every one, which
   quietly broke the search that needs no feature at all: a query ending in an
   extension is two words, and " .pdf" collapsed to ".pdf" glued the extension
   onto the word before it, so `escritura .pdf` searched for `escritura.pdf` and
   found nothing. */
function semanticTextWithoutPeople(query, mentions) {
  let cursor = 0, output = "";
  mentions.forEach(mention => { output += query.slice(cursor, mention.start) + " "; cursor = mention.end; });
  output += query.slice(cursor);
  return output.replace(/\s+/g, " ").replace(/^\s*[’']s\b\s*/i, "")
    .replace(/^\s*(?:and|with|y|con)\b\s*/i, "")
    .replace(/\s+([,.;!?])(?=\s|$)/g, "$1").trim();
}
function semanticComposerText() {
  const composer = document.getElementById("semantic-q");
  return composer ? (composer.textContent || "").replace(/\u00a0/g, " ") : "";
}
function semanticComposerCaret() {
  const composer = document.getElementById("semantic-q"), selection = getSelection();
  if (!composer || !selection.rangeCount || !composer.contains(selection.anchorNode)) return null;
  const range = selection.getRangeAt(0).cloneRange(); range.selectNodeContents(composer);
  range.setEnd(selection.anchorNode, selection.anchorOffset); return range.toString().length;
}
function setSemanticComposerCaret(offset) {
  const composer = document.getElementById("semantic-q"); if (!composer || offset == null) return;
  const range = document.createRange(); let remaining = offset;
  for (const node of composer.childNodes) {
    const length = (node.textContent || "").length;
    if (node.nodeType === Node.TEXT_NODE && remaining <= length) {
      range.setStart(node, remaining); range.collapse(true);
      const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); return;
    }
    if (node.nodeType === Node.ELEMENT_NODE && remaining <= length) {
      // Person tokens are contenteditable=false, so a caret restored inside one
      // leaves the composer focused but unable to accept the next character.
      // Treat the whole token as one atomic item and restore at its edge.
      if (remaining === 0) range.setStartBefore(node);
      else range.setStartAfter(node);
      range.collapse(true);
      const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); return;
    }
    remaining -= length;
  }
  range.selectNodeContents(composer); range.collapse(false);
  const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
}
export function renderSemanticComposer(commitEnd = false) {
  const composer = document.getElementById("semantic-q"); if (!composer || S.composerComposing) return;
  const query = semanticComposerText(), caret = semanticComposerCaret();
  const mentions = extractPeopleMentions(query, (S.filterOpts && S.filterOpts.people) || [], commitEnd)
    .filter(mention => mention.committed);
  const fragment = document.createDocumentFragment(); let cursor = 0;
  mentions.forEach(mention => {
    if (mention.start > cursor) fragment.append(document.createTextNode(query.slice(cursor, mention.start)));
    const token = document.createElement("span");
    token.className = "person-token";
    token.dataset.personId = mention.person.id;
    token.contentEditable = "false";
    token.dataset.tooltip = `Filters to media containing ${mention.person.name}`;
    token.tabIndex = 0;
    token.setAttribute("aria-label", `${mention.person.name}, person filter`);
    token.textContent = mention.source;
    fragment.append(token); cursor = mention.end;
  });
  if (cursor < query.length) fragment.append(document.createTextNode(query.slice(cursor)));
  composer.replaceChildren(fragment);
  if (caret != null) {
    composer.focus({ preventScroll: true });
    setSemanticComposerCaret(caret);
  }
}
export function onSemanticComposerInput() {
  renderSemanticComposer(false);
  // Keep the composer's text in grid state on every keystroke, not just on
  // submit, so leaving the Library and coming back returns a half-typed
  // search instead of an empty box.
  if (S.grid) S.grid.rawQuery = semanticComposerText();
}
export function onSemanticComposerKeydown(event) {
  if (event.key === "Enter") { event.preventDefault(); event.currentTarget.closest("form").requestSubmit(); }
}
export function onSemanticComposerPaste(event) {
  event.preventDefault();
  document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
}
export function setPeopleChecks(prefix, ids) {
  const chosen = new Set(ids.map(String));
  document.querySelectorAll(`#${prefix}-people-filter input[type="checkbox"]`)
    .forEach(input => input.checked = chosen.has(input.value));
}
export async function semanticSubmit(ev) {
  ev.preventDefault();
  const submission = ++SEARCH_SUBMISSION, form = ev.currentTarget;
  const submit = form.querySelector('button[type="submit"]'), oldLabel = submit.textContent;
  const rawQuery = semanticComposerText().trim();
  const g = S.grid;
  const mentions = extractPeopleMentions(rawQuery, (S.filterOpts && S.filterOpts.people) || [], true);
  const mentioned = [...new Set(mentions.map(mention => String(mention.person.id)))];
  const previouslyInferred = new Set((g.inferredPeople || []).map(String));
  const manuallySelected = checkedPeople("f").filter(id => !previouslyInferred.has(String(id)));
  const selected = [...new Set([...manuallySelected, ...mentioned])];
  setPeopleChecks("f", selected);
  g.inferredPeople = mentioned;
  g.people = selected;
  updatePeopleFilterLabel("f", S.filterOpts.people || []);
  const menu = document.getElementById("f-people-filter"); if (menu) menu.removeAttribute("open");
  // The visible sentence stays intact, but recognized names are represented by
  // structured filters and removed from the text that gets embedded.
  g.rawQuery = rawQuery;
  g.searchedQuery = rawQuery;
  // Natural-language image retrieval should not depend on how Caps Lock was
  // used. Keep rawQuery intact for the composer, but normalize the text sent
  // through translation and semantic embedding.
  g.query = semanticTextWithoutPeople(rawQuery, mentions).toLocaleLowerCase();
  /* A new search always lands on the overview, whichever ranking the last one
     was left open at. Which way answers you best is a property of what you
     typed -- the question that put you inside the filenames is not the question
     you are asking now -- so a new one gets the summary of every way and lets
     you choose again.

     Deliberately here and not in `reloadGrids`, which the filters, the sort and
     the result scope all go through as well: narrowing what you are reading is
     not a reason to stop reading it. */
  S.onlyWay = "";
  // Said on screen now rather than when the results land: the way back out is
  // pointing at a set of groups that is already being replaced.
  renderGroupLabels();
  renderSemanticComposer(true);
  renderSortOptions(g);
  renderActiveQuery(g);
  if (submit) { submit.disabled = true; submit.textContent = "Searching…"; }
  // Translation exists to help the *image* model, which was trained
  // overwhelmingly on English. The text index holds whatever language the
  // documents are in and matches it directly, so an archive that only reads
  // its documents must not be made to download 23 MB to search them.
  const expandedQuery = archiveHasFeature(S.arch, "semantic")
    ? await localEnglishTranslation(g.query) : "";
  if (submission !== SEARCH_SUBMISSION || S.grid !== g) return false;
  g.expandedQuery = expandedQuery;
  renderActiveQuery(g);
  if (submit) { submit.disabled = false; submit.textContent = oldLabel; }
  updateClearBtn();
  // A new search is a new thing to read, so it is read from the start. Here for
  // the same reason `S.onlyWay` is cleared here rather than in `reloadGrids`:
  // the filters, the sort and the result scope all go through that too, and
  // only some of them are replacing what you are reading.
  scrollResultsToTop();
  reloadGrids();
  // The ways panel and the result headings are the same list in two states, so
  // the only thing that switches between them is a query arriving or leaving.
  renderSearchWays();
  return false;
}
/* The line under the search box states which search the grid below is
   answering. It is not a copy of the box: the box is a draft the user can
   keep editing, and recognized names are shown as the person filters they
   actually became, so what ran is never in doubt. */
export function renderActiveQuery(g) {
  const el = document.getElementById("active-query"); if (!el) return;
  const searched = (g.searchedQuery || "").trim();
  el.hidden = !searched;
  if (!searched) { el.replaceChildren(); return; }
  const label = document.createElement("span");
  label.className = "aq-label"; label.textContent = "Results for";
  const phrase = document.createElement("span");
  phrase.className = "aq-phrase";
  const mentions = extractPeopleMentions(
    searched, (S.filterOpts && S.filterOpts.people) || [], true)
    .filter(mention => mention.committed);
  let cursor = 0;
  mentions.forEach(mention => {
    if (mention.start > cursor)
      phrase.append(document.createTextNode(searched.slice(cursor, mention.start)));
    const token = document.createElement("span");
    token.className = "person-token";
    token.dataset.tooltip = `Filtered to media containing ${mention.person.name}`;
    token.tabIndex = 0;
    token.setAttribute("aria-label", `${mention.person.name}, person filter`);
    token.textContent = mention.source;
    phrase.append(token); cursor = mention.end;
  });
  if (cursor < searched.length) phrase.append(document.createTextNode(searched.slice(cursor)));
  // The vector search runs on an English rendering of the sentence. Say so
  // on hover when it differs, so an unexpected match has an explanation.
  if (g.expandedQuery && g.query && g.expandedQuery !== g.query)
    phrase.title = `Searched in English as “${g.expandedQuery}”`;
  const clear = document.createElement("button");
  clear.type = "button"; clear.className = "quietbtn sm aq-clear";
  clear.textContent = "Clear search";
  clear.onclick = clearSearch;
  const parts = [label, phrase];
  // Only where there is a ranking to widen. A text match is a match, with no
  // cut to relax, so on a documents-only archive this would name a choice
  // that changes nothing.
  if (g.query && mediaRanksQueries()) parts.push(resultScopeControl(g));
  el.replaceChildren(...parts, clear);
}
/* How much of the ranking is on screen -- two views of one result set, not a
   filter on the library, which is why it sits on the search's own line rather
   than in the filter bar and clears when the search does.

   Two labelled segments rather than a checkbox: "top matches" and "everything"
   are alternatives worth naming, and a checkbox can only name one of them and
   leave the other implied. Only rendered for a description search, since
   browsing has no ranking to widen. */
function resultScopeControl(g) {
  const wrap = document.createElement("span");
  wrap.className = "aq-scope";
  wrap.setAttribute("role", "group");
  wrap.setAttribute("aria-label", "How many results to show");
  const trimmed = g.topMatchesOnly !== false;
  [["Top matches", true, "Only the strong matches for this search"],
    ["All results", false, "Every indexed file, ranked by similarity"]].forEach(
    ([text, wantsTrimmed, hint]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      button.title = hint;
      button.setAttribute("aria-pressed", String(trimmed === wantsTrimmed));
      button.onclick = () => setResultScope(wantsTrimmed);
      wrap.append(button);
    });
  return wrap;
}
async function setResultScope(trimmed) {
  const g = S.grid;
  if (g.topMatchesOnly === trimmed) return;
  g.topMatchesOnly = trimmed;
  renderActiveQuery(g);
  /* Widening the cut does not move the reader.

     "All results" is the same ranking in the same order with the floor taken
     off (routes/search.py drops min_similarity and the relative floor), so
     every result you were already looking at is still there, still in that
     place, with more of them underneath. Being thrown to the top for it was the
     screen answering "show me more" by taking away what you had.

     Put back after the loads rather than in the next frame: the groups are
     emptied and re-fetched, so until the first page lands there is nothing
     under the reader to hold them up and the position clamps to nothing. Deep
     enough in and it still clamps -- only the first page is re-fetched -- but
     the trimmed list this widens is a short one, which is why it is being
     widened. */
    const main = document.getElementById("main");
    const wasAt = main ? main.scrollTop : 0;
    // The scope control only widens the description ranking -- a text match is a
    // match, with no cut to relax -- but both groups reload so their totals stay
    // answers to the same request.
    await reloadGrids();
    if (main) requestAnimationFrame(() => { main.scrollTop = wasAt; });
}
function clearSearch() {
  const composer = document.getElementById("semantic-q");
  if (!composer) return;
  composer.replaceChildren();
  composer.closest("form").requestSubmit();
}
