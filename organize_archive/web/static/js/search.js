// Library search: the contenteditable composer that turns typed names into
// person chips, the local English translation that runs before a description
// search, and the "reach" line that reports how much of the archive a search
// could actually see.

import {
  checkedPeople, loadGrid, renderSortOptions, resetGridResults, updateClearBtn,
  updatePeopleFilterLabel,
} from "./library.js";
import {
  jget,
} from "./api.js";
import {
  S, TYPE_COL,
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
function localTranslator() {
  // The WASM runtime and the es-en model are ~23 MB between them, so whoever
  // asks first waits for the download and the worker spin-up. Building the
  // promise is separated from using it precisely so that cost can be paid
  // ahead of time -- see warmLocalTranslator.
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
// Natural singular/plural label for a media type, so the reach line reads
// "1 video" / "12 videos" rather than a bare type slug.
function reachTypeLabel(type, n) {
  const forms = {
    image: ["image", "images"], video: ["video", "videos"],
    audio: ["audio file", "audio files"], document: ["document", "documents"],
    archive: ["compressed file", "compressed files"], other: ["file", "files"],
  }[type] || [type, type + "s"];
  return forms[n === 1 ? 0 : 1];
}
// Each fresh render supersedes any earlier poll chain (leaving and returning
// to Library must not leave two timers fetching in parallel).
let SEARCH_REACH_GEN = 0;
export function renderSearchReach() { searchReachTick(++SEARCH_REACH_GEN); }
async function searchReachTick(gen) {
  if (gen !== SEARCH_REACH_GEN || S.section !== "library" || !S.arch) return;
  const el = document.getElementById("search-reach"); if (!el) return;
  let s;
  try { s = await jget("/api/browse/semantic/status?root=" + S.arch.id); }
  catch {
    const cur = document.getElementById("search-reach");
    if (cur && gen === SEARCH_REACH_GEN) {
      cur.hidden = false;
      cur.innerHTML = `<span class="reach-note">Description search is unavailable right now.</span>`;
    }
    return;
  }
  if (gen !== SEARCH_REACH_GEN || S.section !== "library") return;
  const cur = document.getElementById("search-reach"); if (!cur) return;
  const by = (s.by_type || []).filter(t => t.count > 0);
  const pending = s.pending || 0;
  let html;
  if (by.length) {
    const chips = by.map(t =>
      `<span class="reach-item"><span class="reach-key" style="background:${TYPE_COL[t.type] || TYPE_COL.other}"></span><b>${t.count.toLocaleString()}</b> ${reachTypeLabel(t.type, t.count)}</span>`
    ).join("");
    // Some of the archive is already searchable; if more is still queued, say
    // so in the same breath rather than a separate alarming line.
    const note = pending ? `searchable by description · ${pending.toLocaleString()} more queued for indexing`
      : "searchable by description";
    html = `${chips}<span class="reach-div" aria-hidden="true"></span><span class="reach-note">${note}</span>`;
  } else if (!s.configured) {
    html = `<span class="reach-note">Search by description isn’t available in this installation.</span>`;
  } else if (pending) {
    // Nothing indexed yet, but work is queued: promise it, with no "0 files"
    // chip — a colour-keyed count of zero has nothing to key.
    html = `<span class="reach-note">No files searchable by description yet · ${pending.toLocaleString()} queued for indexing</span>`;
  } else {
    // Nothing indexed and nothing queued (e.g. an empty archive): no promise to make.
    html = `<span class="reach-note">No files searchable by description yet.</span>`;
  }
  cur.hidden = false;
  cur.innerHTML = html;
  // Indexing runs automatically; keep the counts live until it drains.
  if (s.configured && pending) setTimeout(() => searchReachTick(gen), 2500);
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
function semanticTextWithoutPeople(query, mentions) {
  let cursor = 0, output = "";
  mentions.forEach(mention => { output += query.slice(cursor, mention.start) + " "; cursor = mention.end; });
  output += query.slice(cursor);
  return output.replace(/\s+/g, " ").replace(/^\s*[’']s\b\s*/i, "")
    .replace(/^\s*(?:and|with|y|con)\b\s*/i, "")
    .replace(/\s+([,.;!?])/g, "$1").trim();
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
  renderSemanticComposer(true);
  renderSortOptions(g);
  renderActiveQuery(g);
  if (submit) { submit.disabled = true; submit.textContent = "Searching…"; }
  const expandedQuery = await localEnglishTranslation(g.query);
  if (submission !== SEARCH_SUBMISSION || S.grid !== g) return false;
  g.expandedQuery = expandedQuery;
  renderActiveQuery(g);
  if (submit) { submit.disabled = false; submit.textContent = oldLabel; }
  resetGridResults(g);
  updateClearBtn();
  loadGrid();
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
  clear.type = "button"; clear.className = "linkbtn aq-clear";
  clear.textContent = "Clear search";
  clear.onclick = clearSearch;
  const parts = [label, phrase];
  if (g.query) parts.push(resultScopeControl(g));
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
function setResultScope(trimmed) {
  const g = S.grid;
  if (g.topMatchesOnly === trimmed) return;
  g.topMatchesOnly = trimmed;
  renderActiveQuery(g);
  resetGridResults(g);
  loadGrid();
}
function clearSearch() {
  const composer = document.getElementById("semantic-q");
  if (!composer) return;
  composer.replaceChildren();
  composer.closest("form").requestSubmit();
}
