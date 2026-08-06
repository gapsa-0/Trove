// The rules a feature set obeys, in the one place both screens can read them.
//
// Two screens now decide what an archive runs: the setup screen where one is
// created, and the Features sheet where a live one is changed. They look
// nothing alike, deliberately -- the first sells a decision nobody has made
// yet, the second reports one already made -- but the rules underneath are not
// a matter of presentation. A required feature cannot be removed; one whose
// backend is missing cannot be added; the download figure counts only what is
// not already on disk. Retyping those into the second screen is exactly how it
// would come to disagree with the first, so they are typed once, here.
//
// The catalogue itself was never at risk of drifting: labels, taglines, sizes
// and readiness all come from /api/features (trove/features.py), and both
// screens read the same answer. What lives here is only what the frontend
// decides for itself.

export function canAdd(feature, chosen) {
  return !!feature && feature.available && !chosen.has(feature.id);
}
export function canRemove(feature, chosen) {
  return !!feature && !feature.required && chosen.has(feature.id);
}

// What a feature costs to switch on, in as few words as it deserves, and which
// of three answers it is.
//
// The three are worth telling apart at a glance, because only one of them is a
// reason to hesitate: "300 MB" is a bill, "Downloaded" says you already paid it
// for another archive, and "No download needed" says there was never one to
// pay. Bare "no download" read as a fragment of a sentence someone had cut off
// -- no download *what?* -- so it says what it means.
//
// The `tone` is what the two screens colour by; both spell it into a class of
// the same name, and neither decides for itself what green means.
export function cost(feature) {
  if (!feature.download_mb) return { text: "No download needed", tone: "free" };
  // A feature whose backend is missing is not quoting a bill anyone can be
  // charged: nothing on this screen can start that download, so the figure is
  // information and takes no tone. Colouring it like a cost would put the
  // loudest label on the card the archive can do least about.
  if (!feature.available) return { text: `${feature.download_mb} MB`, tone: "" };
  if (feature.ready) return { text: "Downloaded", tone: "ready" };
  return { text: `${feature.download_mb} MB`, tone: "cost" };
}

// The class that spells a tone, with no trailing space when there is none.
export function costClass(feature) {
  return ["set-cost", cost(feature).tone].filter(Boolean).join(" ");
}

// What this feature set will actually download. A feature whose weights are
// already on disk contributes nothing, which is the difference between an
// honest figure and a scary one.
export function pendingDownloadMb(catalogue, chosen) {
  return catalogue
    .filter(f => chosen.has(f.id) && !f.ready)
    .reduce((total, f) => total + f.download_mb, 0);
}

// Half of a pair running without the other half, as [lonely, partner], or null
// when there is no such gap. Both halves name each other, so a caller says this
// once about the whole set rather than twice, once on each feature.
export function lonelyPair(catalogue, chosen) {
  for (const feature of catalogue) {
    if (!chosen.has(feature.id) || !feature.pairs_with) continue;
    if (chosen.has(feature.pairs_with)) continue;
    const partner = catalogue.find(f => f.id === feature.pairs_with);
    if (partner && partner.available) return [feature, partner];
  }
  return null;
}
