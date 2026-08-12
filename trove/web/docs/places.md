---
title: Places
summary: Turning GPS coordinates into the handful of places you keep going back to.
feature: places
---

{{tagline}}. Photos that already carry GPS coordinates are gathered into
places: the house, the office, the beach you return to every summer. You name
them, pin them, and correct them.

This is the only stage with no model in it at all. It is geometry.

## How it works

**The coordinates.** Nothing here looks up an address or contacts a mapping
service. The only input is the GPS tags already written into your photos by the
camera or phone that took them, read during [indexing](indexing.md). A photo
without coordinates is not placed, though you can add it to a place by hand.

**Clustering.** Every geotagged photo is a point. Two points within 300 metres
of each other belong to the same place, and that relation is closed
transitively with a union-find structure, so a walk down a street becomes one
place rather than a chain of separate ones.

Distance is the haversine formula on a spherical earth, which is accurate to
well within a metre at these scales. Comparing every point to every other would
not scale, so points are first bucketed into a grid sized to the radius, and
only points in neighbouring cells are measured, which keeps a dense hotspot
(three hundred photos taken in one room) cheap.

**Places are durable.** The clustering pass above is a one-time bootstrap, run
only when an archive has no places yet. After that, newly geotagged photos are
assigned incrementally to the nearest existing place within 300 metres, or
start a new one. Existing places, their names, their pins and anything you
attached by hand are never rebuilt or discarded.

**The reporting floor.** A one-off snapshot at a shop or a stranger's house
should not earn "place" status just because a photo happened to have
coordinates there. So a place with fewer than 10 photos is not *shown*. It
still exists, it is simply not reported, and it starts appearing the moment it
grows. Anything you have named yourself, or pinned, or created by hand is
exempt: naming it is what makes it intentional.

Photos in a below-floor place show as having no location. That is deliberate,
not a gap.

## What you can do on the map

**Name a place.** A new place is a pin and a photo count. Click it and type
what it is. Naming a place also exempts it from the reporting floor below, so a
place you have named never disappears for being small.

**Merge two places.** Drag one pin onto another. Trove asks first, and if the
two are further apart than 20 km it says so, because that is well past the
distance a genuine "one place got split in two" merge covers. Nothing is
refused, and a merge can be undone.

**Or merge by name.** Every card's ⋯ menu, and an open place's panel, offers
"Merge with…" and a list of the places you have already named — for when the
two are not on screen together.

**Create a place by hand,** and add photos to it that carry no coordinates of
their own. This is how scanned photos and anything that came through a
messaging app get onto the map at all.

**Turn the street map on or off.** Off, the map is your photos plotted on an
empty ground and nothing leaves the machine. On, it fetches map tiles from a
public server, which tells that server which area you are looking at. Never
your photos, and it is the only outbound call in the app that depends on your
own data.

## The numbers

| Setting | Default | What it does |
| --- | --- | --- |
| `place_min_media` | 10 | Photos a place needs before it is shown; named and pinned places are exempt |
| `place_merge_warn_km` | 20.0 | Spread past which a drag-merge asks you to confirm first |
| Not a setting | 300 m | Clustering radius: the distance within which two photos are the same place |

The merge warning is not a limit. Nothing is ever refused for being too spread
out, and two places on opposite sides of the country will still merge if you
ask. It exists because the clustering radius is 300 metres, so a genuine "one
place got split in two" merge is almost always under a kilometre, and anything
much larger is worth a second look.

## What runs on your machine

Nothing is downloaded and no model is loaded. The map tiles are drawn by
Leaflet, which ships with the app.

| Component | Used for | Downloaded |
| --- | --- | --- |
| Haversine distance and union-find | Grouping points into places | None |
| Leaflet | Drawing the map | None |

## What it gets wrong

**Everything depends on the photos having GPS at all.** Scanned photos, screen
grabs, anything through a messaging app that strips metadata, and every camera
without a GPS chip contribute nothing. On many archives most files have no
coordinates, and Places is correspondingly thin.

**300 metres is one number for two very different situations.** In a dense city
it merges a café, the flat above it and the park across the road into one
place. In open country it splits a single large site, such as a farm, a
campsite or a long beach, into several. There is no radius right for both.

**Drift and cached fixes.** A phone that recorded a stale GPS fix, or one
indoors relying on wifi positioning, can place a photo hundreds of metres from
where it was taken. Those photos land in a neighbouring place, or start a
spurious one that the reporting floor usually hides.

**A place is where photos were taken, not what is there.** Trove has no
gazetteer and does not know a place's name. Until you name one, it is a pin and
a photo count.
