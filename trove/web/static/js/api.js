// Every call to the backend goes through here. Reads are plain fetches; writes
// are queued, for the reason the comment on the queue explains.

export async function jget(u) { return (await fetch(u, { cache: "no-store" })).json(); }
export async function jpost(u, b) { return (await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) })).json(); }
// Serialize all persistence through ONE in-flight write. A GUI write can block for
// a few seconds waiting for the background pipeline's single SQLite writer; firing
// several at once ties up the browser's ~6 connections with stalled POSTs and the
// whole app hangs (reads/polling/images can't get a connection). Chaining keeps at
// most one write occupying a connection, so reads always stay responsive. The
// optimistic UI has already updated, so queueing the actual write is invisible.
let _wq = Promise.resolve();
export function qpost(u, b) {
  const run = () => jpost(u, b);
  const p = _wq.then(run, run);      // run regardless of the previous write's outcome
  _wq = p.catch(() => { });            // a rejection must not break the chain
  return p;
}
// The same budget, from the read side: wrap a poll tick so a slow answer is
// waited for rather than piled on. A tick on a fixed interval assumes it
// finishes within one -- and the pipeline snapshot does not, because the first
// one after an archive is opened waits for its tree to be counted (~20s for 97k
// files on a cold cache). Every interval that passes meanwhile used to add
// another request, and a handful of those is the whole connection budget, so
// thumbnails, library pages and search stopped being answered too: the archive
// looked frozen rather than merely slow to say what it was doing.
//
// Skips the tick outright instead of queueing it, which is what separates this
// from `qpost`: a status poll carries no user intent, and the run already in
// flight is about to return the same answer a queued one would.
export function oneAtATime(tick) {
  let running = false;
  return async (...args) => {
    if (running) return;
    running = true;
    try { return await tick(...args); } finally { running = false; }
  };
}
// Whether a pipeline snapshot is about the archive that is open *now*.
//
// A poll started before the user switched archives still lands afterwards, and
// nothing checked whose answer it was: the cards, the sidebar chip and the
// people/pets progress panels would all take it, so the archive you had just
// opened reported the one you left -- its stages, its counts, its pause state.
// Rare while the snapshot was fast, but the walk this budget is all about makes
// the window 20s wide, and switching away from a slow archive is exactly what
// someone does about one.
//
// Asked of the payload's own `root_id` rather than an id captured before the
// await, because that is the server saying which archive it answered about --
// the captured-id version cannot tell a stale reply from a current one when the
// same archive is reopened.
export function isCurrentSnapshot(snap, arch) {
  return !!(snap && arch && snap.root_id === arch.id);
}
