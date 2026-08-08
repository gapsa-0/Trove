// The one place `/api/pipeline` is fetched, and the one place `S.pipeline` is
// written. Everything that draws pipeline state subscribes here.
//
// It used to be four tickers -- the Overview's cards, the sidebar chip, and the
// People and Pets progress panels -- each on its own interval, three of them
// assigning `S.pipeline`, and two of them asking for a snapshot the other two
// already had. That cost real requests: a browser allows about six connections
// per origin, and People spent two of them per tick on a resource it did not
// need to ask for. It also meant every guard had to exist four times, which is
// how a snapshot belonging to an archive the user had already left could be
// taken by one surface and ignored by another.
//
// One fetch, one place to guard it, and subscribers that are told rather than
// asking. The interval is the fastest any of the four used to run, so nothing
// refreshes more slowly than before while the total request count drops.

import {
  isCurrentSnapshot, jget, oneAtATime,
} from "./api.js";
import {
  S,
} from "./state.js";

const POLL_MS = 1200;
const subscribers = new Set();

/* Draw whenever a snapshot lands. Returns its own unsubscribe, though the
   screens here never need it: a subscriber whose DOM is not on screen finds
   nothing to write to and returns, which is the same check it needed anyway
   for the poll that used to run past its own section. */
export function onSnapshot(fn) {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

// Wrapped so a tick that outlasts its interval is waited for rather than piled
// on. Without it, a snapshot that takes its time -- the walk behind a freshly
// opened archive used to take ~20s -- collects one stalled request per interval
// until the connection budget is gone and no other request on the page can be
// sent at all. See `oneAtATime`.
const tick = oneAtATime(async () => {
  if (!S.arch) { stopPipelinePoll(); return; }
  let snap;
  try { snap = await jget("/api/pipeline?root=" + S.arch.id); }
  catch { return; }   // transient; the next tick retries
  // A poll started before the user switched archives still lands afterwards.
  // Asked of the payload's own root_id -- the server saying which archive it
  // answered about -- rather than an id captured before the await, which cannot
  // tell a stale reply from a current one across a reopen.
  if (!isCurrentSnapshot(snap, S.arch)) return;
  S.pipeline = snap;
  // Copied first: a subscriber is free to unsubscribe from inside its own
  // callback. Each is isolated, so one screen throwing cannot stop the chip
  // beside it from updating.
  for (const fn of [...subscribers]) {
    try { fn(snap); } catch { /* one bad subscriber must not silence the rest */ }
  }
});

export function startPipelinePoll() {
  stopPipelinePoll();
  S.gpoll = setInterval(tick, POLL_MS);
  tick();
}
export function stopPipelinePoll() {
  if (S.gpoll) { clearInterval(S.gpoll); S.gpoll = null; }
}
/* Ask now rather than up to a poll away. Used after a write that changes what
   the pipeline is doing (a pause, a returned window), so the surfaces move as
   soon as the server has something to say. */
export function refreshPipelineNow() { return tick(); }
