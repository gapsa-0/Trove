// The reference screen: how each stage of the pipeline actually works.
//
// A top-level screen rather than a nav section, and deliberately so. Every item
// in the archive sidebar is a view onto *this archive's data*; these pages are
// about the app and read the same whichever folder is open. They are also worth
// reading BEFORE a feature is switched on -- the setup panel is where someone
// decides whether to spend 689 MB on search -- and a section inside the archive
// shell is invisible at exactly that moment. So this sits beside #picker and
// #setup, and every screen that has a stage behind it offers a way in.
//
// Pages are Markdown files under web/docs/, rendered server-side (web/docs.py).

import {
  jget,
} from "./api.js";
import {
  esc,
} from "./dom.js";
import {
  ICONS, S,
} from "./state.js";

// Which page a section's info button opens. The Timeline opens Indexing: it is
// a view of what that stage produced, and saying so is more honest than writing
// the same page twice.
//
// Browse used to do the same, which was right when the only thing to explain
// about it was where the files came from. It is now the one screen with five
// readers behind it, and a button labelled "How search works" that opened a
// page about scanning was answering a question nobody had asked.
const DOC_FOR_SECTION = {
  overview: "index", library: "browse", timeline: "indexing",
  people: "people", pets: "pets", places: "places", dups: "duplicates",
};
// Which page a feature's "How it works" link opens, for the setup panel. Two
// tables rather than one because they answer different questions -- a section
// is a place in the app, a feature is a piece of work, and the Timeline and
// Browse share one feature between two sections.
//
// Every feature is listed. The two that arrived with document text were not,
// so choosing them on the setup panel was the one decision made with no way to
// read what it does first.
const DOC_FOR_FEATURE = {
  index: "indexing", duplicates: "duplicates", people: "people",
  pets: "pets", places: "places", semantic: "search",
  documents: "documents", ocr: "ocr",
};
const DOCS = {
  pages: [],
  slug: "",
  // What was showing when the reader opened these pages, so closing puts them
  // back exactly there. The screen has to be recorded as well as the hash:
  // setup is a screen with no route of its own, and reconstructing "wherever
  // the hash points" would drop someone mid-setup onto the start page with
  // their half-made choices apparently gone.
  from: "",
  fromScreen: "",
  spy: null,
};

// Asked of the DOM rather than of the modules that own these screens, so that
// this one can be imported by all of them without importing any of them back.
function currentScreen() {
  // Before the archive shell, not after: the Features sheet is a layer over an
  // open archive, so "app" is true underneath it and would win.
  if (document.getElementById("features-sheet").classList.contains("open")) return "features";
  if (document.getElementById("setup").style.display === "block") return "setup";
  if (document.getElementById("app").classList.contains("on")) return "app";
  return "picker";
}
// The sheet is a fixed overlay, so hiding the archive beneath it does not hide
// it: without this, a feature's documentation would open underneath its own
// sheet. Closed rather than destroyed, exactly like the setup screen, so what
// was switched is still switched on the way back.
function setFeatureSheetShown(on) {
  document.getElementById("features-sheet").classList.toggle("open", on);
  document.getElementById("features-backdrop").classList.toggle("open", on);
  document.getElementById("features-sheet").setAttribute("aria-hidden", on ? "false" : "true");
}

// The mark a screen carries to its own documentation. Quiet on purpose: it is
// an offer, not something the screen wants you to press. Lives in .pagehead's
// right-hand slot, which is already a space-between flex row.
export function docsButton(section) {
  const slug = DOC_FOR_SECTION[section];
  if (!slug) return "";
  return `<button type="button" class="quietbtn doc-info" onclick="openDocs('${slug}')"
      aria-label="How this works">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/>
        <path d="M12 11v5"/><path d="M12 7.6v.6"/></svg>
      <span>How this works</span></button>`;
}

export async function openDocs(slug) {
  if (!docsOpen()) {
    DOCS.from = location.hash || "";
    DOCS.fromScreen = currentScreen();
  }
  document.getElementById("picker").style.display = "none";
  document.getElementById("setup").style.display = "none";
  document.getElementById("app").classList.remove("on");
  setFeatureSheetShown(false);
  const screen = document.getElementById("docs");
  // Both fetches below are local, so this is on screen for a frame or two --
  // but showing the app's own background rather than nothing is what stops the
  // first press reading as a click that did nothing.
  if (!screen.childElementCount) screen.innerHTML = '<div class="doc-wait">Loading…</div>';
  screen.classList.add("on");
  if (!DOCS.pages.length) {
    const res = await jget("/api/docs").catch(() => ({ pages: [] }));
    DOCS.pages = res.pages || [];
  }
  await showDoc(slug || "index");
}

export function closeDocs() {
  document.getElementById("docs").classList.remove("on");
  stopSpy();
  const back = DOCS.from && !isDocsHash(DOCS.from) ? DOCS.from : "";
  const screen = DOCS.fromScreen;
  DOCS.from = ""; DOCS.fromScreen = "";
  // Nothing here touched the screen underneath -- the archive shell still holds
  // its stashed section DOM, and the setup panel still holds what was typed and
  // chosen -- so going back is showing it again. The section poll was left
  // running for the same reason: it makes returning instant, and it is one
  // request a second against a server on this machine.
  if (screen === "setup") {
    document.getElementById("setup").style.display = "block";
    // Setup has no route of its own, so there is no hash that means "the setup
    // panel". Clearing it is the honest answer: the route now names no screen,
    // and closing setup goes on to the start page as it always did.
    location.hash = "";
    return;
  }
  if ((screen === "app" || screen === "features") && S.arch) {
    document.getElementById("app").classList.add("on");
    location.hash = back || `/archive/${S.arch.id}/${S.section}`;
    if (screen === "features") setFeatureSheetShown(true);
    return;
  }
  document.getElementById("picker").style.display = "";
  location.hash = "";
}

// The link a feature's card carries to its own page. Offered on the back of the
// card -- the face someone turned over to ask "what is this" -- rather than on
// the front, where it would compete with the choice the screen is there to make.
export function featureDocsLink(featureId) {
  const slug = DOC_FOR_FEATURE[featureId];
  if (!slug) return "";
  // Stopped, because this button sits on a card whose whole face is a switch:
  // without it, reading the page also toggled the feature it describes.
  return `<button type="button" class="doc-more"
      onclick="event.stopPropagation();openDocs('${slug}')">
      How it works</button>`;
}

// `#/docs`, `#/docs/duplicates` -> the slug ("index" when none is named), or
// null when this hash names something else. The router asks before it tries to
// resolve an archive, and closeDocs asks so it never "returns" to itself.
export function docsHashSlug(hash) {
  const m = /^#?\/docs(?:\/([a-z0-9-]+))?\/?$/.exec(hash || "");
  return m ? (m[1] || "index") : null;
}
function isDocsHash(hash) { return docsHashSlug(hash) !== null; }
export function docsOpen() { return document.getElementById("docs").classList.contains("on"); }
export function docsSlug() { return DOCS.slug; }

export async function showDoc(slug) {
  const screen = document.getElementById("docs");
  const known = DOCS.pages.some(p => p.slug === slug);
  DOCS.slug = known ? slug : "index";
  location.hash = `/docs/${DOCS.slug}`;
  const page = await jget(`/api/docs/page?slug=${encodeURIComponent(DOCS.slug)}`)
    .catch(() => null);
  if (!page || page.error) {
    screen.innerHTML = rail() + `<article class="doc-article">
      <h1>Page not found</h1>
      <p class="doc-lede">There is no reference page called “${esc(slug)}”.</p></article>`;
    return;
  }
  screen.innerHTML = rail() + article(page) + outline(page);
  screen.scrollTop = 0;
  revealScales(screen);
  startSpy(screen, page.outline || []);
}

function rail() {
  const link = p => `<button type="button" class="doc-link${p.slug === DOCS.slug ? " active" : ""}${p.always_runs ? " always" : ""}"
      onclick="showDoc('${p.slug}')" title="${esc(p.summary)}">${esc(p.title)}</button>`;
  // Two groups, because they are two different kinds of page: the stages, in
  // the order they run and joined as the chain they are, and the two that
  // belong to no stage.
  const chain = DOCS.pages.filter(p => p.feature);
  const loose = DOCS.pages.filter(p => !p.feature);
  return `<nav class="doc-rail" aria-label="Reference pages">
      <button type="button" class="doc-rail-back" onclick="closeDocs()">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
        <span>Back to the app</span></button>
      <div class="doc-rail-title">Reference</div>
      ${loose.filter(p => p.slug === "index").map(link).join("")}
      <div class="doc-chain">${chain.map(link).join("")}</div>
      <div class="doc-rail-loose">${loose.filter(p => p.slug !== "index").map(link).join("")}</div>
      <div class="doc-rail-key">
        <span><i class="filled"></i>Always runs</span>
        <span><i></i>Chosen per archive</span>
      </div>
    </nav>`;
}

function article(page) {
  // The eyebrow states where this page sits: which feature it documents, its
  // mark, and whether the work is optional. All of it comes from the feature
  // catalogue, so this page is visibly the same thing as the setup card and
  // the Overview card that share its name.
  const mark = page.icon && ICONS[page.icon]
    ? `<span class="feat-mark" aria-hidden="true">${ICONS[page.icon]}</span>` : "";
  const eyebrow = page.feature
    ? `<div class="doc-eyebrow">${mark}<span>${esc(page.feature_label)}</span>
        <span class="sep">/</span>
        <span class="${page.always_runs ? "" : "opt"}">${page.always_runs ? "Always runs" : "Optional"}</span>
        ${page.download_mb ? `<span class="sep">/</span><span class="opt">${page.download_mb} MB</span>` : ""}
       </div>`
    : `<div class="doc-eyebrow"><span>Reference</span></div>`;
  const step = to => {
    const p = DOCS.pages.find(x => x.slug === to);
    return p ? p.title : "";
  };
  const nextTitle = step(page.next), prevTitle = step(page.prev);
  const nav = nextTitle || prevTitle ? `<div class="doc-next">
      ${prevTitle ? `<button type="button" class="prev" onclick="showDoc('${page.prev}')">
        <span class="dir">Previous</span><span class="to">${esc(prevTitle)}</span></button>` : ""}
      ${nextTitle ? `<button type="button" onclick="showDoc('${page.next}')">
        <span class="dir">Next</span><span class="to">${esc(nextTitle)}</span></button>` : ""}
    </div>` : "";
  // The slug is on the article so a page whose headings are a different KIND of
  // thing can say so in CSS. The FAQ is the case: its h2s are questions, and
  // the small mono-uppercase label that suits "How it works" reads as shouting
  // when it is a whole sentence ending in a question mark.
  return `<article class="doc-article" data-doc="${esc(page.slug)}">
      ${eyebrow}
      <h1>${esc(page.title)}</h1>
      ${page.summary ? `<p class="doc-lede">${esc(page.summary)}</p>` : ""}
      <div class="doc-body">${page.html}</div>
      ${nav}
    </article>`;
}

function outline(page) {
  const items = page.outline || [];
  if (items.length < 2) return `<div class="doc-outline"></div>`;
  return `<aside class="doc-outline" aria-label="On this page">
      <div class="doc-outline-title">On this page</div>
      ${items.map(h => `<a href="#/docs/${DOCS.slug}" data-goto="${esc(h.id)}">${esc(h.text)}</a>`).join("")}
    </aside>`;
}

// The one animated thing on this screen: each calibration figure's bands grow
// from the left once, which reads the scale in the direction the numbers run.
// Reduced motion is honoured in the stylesheet (transition: none), so this
// still resolves to the finished state there.
function revealScales(screen) {
  const bands = [...screen.querySelectorAll(".doc-scale-band")];
  if (!bands.length) return;
  bands.forEach(b => { b.style.transform = "scaleX(0)"; });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    bands.forEach(b => { b.style.transform = ""; });
  }));
}

function stopSpy() {
  if (!DOCS.spy) return;
  DOCS.spy.el.removeEventListener("scroll", DOCS.spy.fn);
  DOCS.spy = null;
}
// Which heading the reader is under. A scroll listener rather than an
// IntersectionObserver because the question is "which is the last one above the
// fold", which an observer answers only indirectly and gets wrong for a heading
// taller than the viewport.
function startSpy(screen, items) {
  stopSpy();
  if (items.length < 2) return;
  const links = [...screen.querySelectorAll(".doc-outline a")];
  const fn = () => {
    const top = screen.getBoundingClientRect().top + 90;
    let at = 0;
    items.forEach((h, i) => {
      const node = document.getElementById(h.id);
      if (node && node.getBoundingClientRect().top <= top) at = i;
    });
    links.forEach((a, i) => a.classList.toggle("here", i === at));
  };
  screen.addEventListener("scroll", fn, { passive: true });
  DOCS.spy = { el: screen, fn };
  fn();
}

// In-page navigation, for both the outline rail and any anchor a page writes
// itself. Handled here instead of by the browser because the location hash is
// this app's route -- letting `#the-numbers` land in it would navigate away
// from the page the reader is on.
document.addEventListener("click", event => {
  const link = event.target.closest?.("#docs a[href^='#']");
  if (!link) return;
  const target = link.dataset.goto || link.getAttribute("href").replace(/^#/, "");
  if (target.startsWith("/docs/")) return;          // a real route: let it route
  const node = document.getElementById(target);
  if (!node) return;
  event.preventDefault();
  node.scrollIntoView({ behavior: "smooth", block: "start" });
});
