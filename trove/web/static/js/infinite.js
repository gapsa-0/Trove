import {
  S,
} from "./state.js";

/* ---------- generic forward-only infinite scroll ----------
   Every catalog list outside the Library (Duplicates, People, Pets, and
   their detail grids) is always entered at offset 0 and only grows
   downward -- unlike the Library grid's filtered/date-jump entry points,
   so a single bottom sentinel is enough; no prepend, no anchor restore.
   Mirrors loadGrid()'s auto-refill-while-visible behavior.
   `stateKey` names an S.<key> slot: starting a new list for the same key
   disconnects the previous observer and makes any of its still-in-flight
   fetch a no-op once it lands (the same staleness guard the Library grid
   gets from `S.grid !== g`), so a merge/reload can't paint stale cards
   into a grid that has since been reset. */
export function startInfiniteList(stateKey, { sentinelId, pageSize, fetchPage, onPage, root }) {
  if (S[stateKey] && S[stateKey].observer) S[stateKey].observer.disconnect();
  const scrollRoot = root || document.getElementById("main");
  const sentinelEl = () => document.getElementById(sentinelId);
  function isNear() {
    const s = sentinelEl();
    if (!s || !scrollRoot || !s.isConnected) return false;
    const sr = s.getBoundingClientRect(), rr = scrollRoot.getBoundingClientRect();
    return sr.top <= rr.bottom + 600;
  }
  const state = { offset: 0, done: false, loading: false, observer: null };
  async function loadMore() {
    if (S[stateKey] !== state || state.done || state.loading) return;
    const first = state.offset === 0;
    state.loading = true;
    let s = sentinelEl(); if (s) s.innerHTML = '<span class="spin"></span>Loading…';
    let failed = false;
    try {
      const items = await fetchPage(state.offset, pageSize);
      if (S[stateKey] !== state) return;
      state.offset += items.length;
      state.done = items.length < pageSize;
      onPage(items, { first, done: state.done });
      s = sentinelEl(); if (s) s.textContent = "";
    } catch {
      failed = true;
      s = sentinelEl();
      if (S[stateKey] === state && s) s.textContent = "Couldn’t load more. Scroll away and back to retry.";
    } finally {
      state.loading = false;
      requestAnimationFrame(() => {
        if (!failed && S[stateKey] === state && !state.done && isNear()) loadMore();
      });
    }
  }
  const sentinel = sentinelEl();
  if (sentinel) {
    state.observer = new IntersectionObserver(entries => {
      if (S[stateKey] !== state || state.done) return;
      if (entries.some(entry => entry.isIntersecting)) loadMore();
    }, { root: scrollRoot, rootMargin: "600px 0px" });
    state.observer.observe(sentinel);
  }
  S[stateKey] = state;
  loadMore();
  return state;
}
// Every S.<key> a startInfiniteList() call site uses; swept on archive
// switch (resetSectionViews) so no orphaned observer keeps a detached
// sentinel from a closed archive alive.
export const INFINITE_LIST_KEYS = [
  "dupList", "peopleList", "personDetailList",
  "petListState", "loosePetState", "nonhumanState", "petDetailList", "placeList",
];
