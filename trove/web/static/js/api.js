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
