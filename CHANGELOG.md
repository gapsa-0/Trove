# Changelog

All notable user-facing changes to Trove are documented in this file.

Trove ships as a desktop app with an installer, not a library people pull with a
package manager — for someone on an older version deciding whether to download a
newer one, this file (and the GitHub release notes generated from it) is the only
account of what changed. Internal restructuring, typing and lint work is summarised
under `Internal` rather than itemised: it has no user-visible effect and belongs in
`ARCHITECTURE.md` for contributors, not here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.1] - 2026-09-04

### Fixed

- **The AppImage starts on Ubuntu 23.10 and later.** Trove's window is Chromium,
  which sandboxes the part of itself that renders your media using a facility
  Ubuntu 23.10 began withholding from programs no AppArmor profile covers. An
  AppImage installs nothing, so nothing can give it a profile, and 0.3.0's
  answered that by exiting with a message about `chrome-sandbox` where a window
  should have been. It now recognises that situation at launch and starts with
  the renderer sandbox off, saying so in Help → Copy diagnostics and on stderr.
  Where a sandbox is available — every distribution that does not restrict user
  namespaces — nothing changes. The `.deb` was never affected, because it
  installs a profile of its own; on Ubuntu it remains the better download, and
  the Linux install notes now say why.

- **The installers stopped carrying a graphics toolkit nothing uses.** The two
  packages behind face and text detection both ask for the desktop build of
  OpenCV, so it was installed beside the windowless build Trove pins and the
  frozen app took whichever arrived last. 0.3.0 took the desktop one and shipped
  Qt5, an X11 plugin stack and a second copy of FFmpeg with it — 36 MB of files
  no part of Trove can reach. Every download is smaller for their absence: the
  AppImage by 14 MB, the `.deb` by 11 MB.

- **Windows: the FFmpeg libraries were in the installer twice.** 177 MB of them,
  about 50 MB of the download, because the freezer keeps its own copy of a
  bundled library it also finds as a dependency, and Windows has no symlink to
  collapse the two into one. Linux was never affected. Nothing loaded the second
  copy; it was only ever weight.

### Internal

- The build now refuses to package OpenCV's desktop build or a duplicated
  FFmpeg, rather than trusting the environment it runs in to be right.
- A workflow installs a published Windows release on a Windows runner, launches
  it and waits for its catalogue service to answer — the half of the release
  checklist that a maintainer with no Windows machine could not otherwise do.

## [0.3.0] - 2026-09-03

### Added

- **Appearance follows the computer, or doesn't, and says which.** The settings
  drawer held one button reading "Dark appearance", which showed no state and
  could not be told to go back to following the computer: every new install
  started out following it, and the first press wrote a choice nothing removed.
  There are three named options now — System, Light, Dark — in the shape
  Browse's result scope already uses, with the one you are on lit. Choosing
  System forgets the stored value, and the app follows the computer as it
  changes rather than only at startup.

- **Browse can be filtered by pet.** The filter bar could narrow the grid to a
  person but not to an animal, though the two are the same question. It now
  offers your named pets beside your named people, and behaves identically:
  photos you tagged by hand count even where nothing was detected, and picking
  two asks for both in the same photo.

- **Merge a group by picking a name, not by dragging.** Dragging one card onto
  another needs both on screen, which stops being possible at a few hundred
  groups — and the common case is precisely that you recognise a group of
  strangers as somebody you named long ago, whose card is nowhere near. Every
  card's ⋯ menu and every group's own page now offers "Merge with…" and a list
  of the groups you have already named. On People, Pets and Places.

- **Pets get everything People has for photos.** A pet's card showed a single
  thumbnail, and its photos carried no controls at all. Cards now show a collage
  of up to four, each photo offers "Make cover photo" and "This is not the pet",
  and both choices survive re-clustering. Removing a pet's last photo removes
  the group, as it already did for a person.

- **The cover photo you choose is the one you see.** Choosing one saved
  correctly and then displayed nothing: a person's page drew its portrait from
  the first photo in the list rather than from the cover, and their card in the
  grid drew a collage ranked purely by sharpness. Both now lead with the face
  you picked.

- **You choose which photo represents a person.** Trove picked whichever face it
  judged sharpest, which is not always the one that looks like them, and there
  was no way to say otherwise. Every photo on a person's page now offers "Make
  cover photo", and the choice outranks the automatic pick from then on —
  including across re-clustering, which previously overwrote the card's face
  every time it ran. The photo's other control, "This is not the person", is the
  same one that was there before, now labelled rather than a bare ✕.

- **Groups can be hidden, for either of the two reasons people hide them.** The
  People screen offered one way out of a crowded grid — "Not a person" — which
  marks the faces as a doll or an animal and takes them out of grouping for
  good. That is the wrong thing to say about a stranger in the background of a
  party photo, and there was nothing else to say. Every card and every person's
  page now carries both: *Not a person*, unchanged, and *Unknown person*, which
  hides a real group while leaving its faces grouping exactly as before. Hidden
  groups gather at the foot of the screen and come back with one click. (The
  old control was also unusable in the desktop app, for the same reason pet
  renaming was.)

- **A record of what you changed, on every person and pet.** Their pages used to
  carry a list of past merges wedged between the name and the photographs — the
  account of the work sitting on top of the work, and only ever about merges.
  That list is now a clock in the top bar, and it covers the rest of it: names,
  photos added or dropped by hand, and merges, newest first, each offering to
  undo itself. Undoing marks an entry rather than deleting it, because a history
  that erased itself as you used it would be a poor account of the afternoon.

- **Tab saves a name and opens the next one.** Naming a screenful of groups is
  what the People, Pets and Places grids are for, and reaching for the mouse
  between every one of them was most of the work: Tab moved the focus to the
  card's own actions menu instead. It now commits what you typed and opens the
  editor on the card beside it, Shift+Tab on the one before.

- **A place's name can be taken back off, like a person's or a pet's.** The
  Places grid had its own copy of the name editor rather than the shared one, so
  it never got the "Remove name" the others have — even though an unnamed place
  is a state Trove has always been able to store.

- **Name someone from the photograph they are in.** The panel beside a photo
  could only ever point a face at somebody you had already named somewhere else
  — so on a fresh archive it had nothing to offer but a sentence sending you to
  the People screen, and a face too alone to have formed a group could not be
  named from anywhere at all. Every unnamed face now carries *Name*, and typing
  there does the right one of three things: names the group the face is in, puts
  the face with the person who already has that name, or makes a person for a
  face that had none. The name sticks through re-clustering either way.

- **Duplicates says what the copies are in a line, not a panel of charts.** The
  three stacked bars between the tiles and the list were mostly saying things
  said elsewhere: what kind of file the unique ones are is the Overview's
  storage panel, one screen away, and it cost a pass over every file in the
  archive on every refresh to repeat it. The one thing only this screen knows —
  how many of the copies are byte-identical and how many are merely the same
  picture saved differently — now sits under the *Redundant copies* count it
  qualifies.

- **One pet, one name.** Grouping rebuilds every pet after each batch of photos,
  and each new group took the name of any old pet it shared a photo with — so a
  cat whose photos split into two groups came back as two cats called Kira, and
  a name could wander onto a group that had swept up a single stray photograph
  of it. A name now goes to the one group that best inherited it, as it already
  did for people.

- **A name you give a small group is not lost the next time faces are grouped.**
  Grouping rebuilds every person from scratch each time it runs, and a name
  survived only if the pass could work out which new group inherited the old one
  — a guess that needs three photographs of overlap and finds none at all for a
  group whose faces no longer group together, which is exactly the kind you
  named by hand. Naming a group now anchors the name to the face on its card, so
  the name comes back whatever grouping does. Taking the name off releases the
  anchor with it.

- **Keep more than one copy of a duplicate group.** Trove picks the copy it
  judges best and hides the rest, which is a good default and a bad rule: the
  "worse" copy is sometimes the one already in the album you share, and two
  copies of what grouping called the same picture are sometimes two pictures.
  Every copy on the Duplicates screen now carries *Keep*, and any set of them
  can be the one Browse shows — including one that leaves out the copy Trove
  picked. A group always keeps at least one, so nothing can vanish from Browse
  with nowhere to say where it went, and the choice survives the next grouping
  run, which rebuilds every group from scratch.

- **Do something to several groups at once.** A party leaves thirty strangers on
  the People screen, and they are thirty of the same decision — but every way of
  acting on a group worked on exactly one of them. *Select* on People, Pets and
  Places turns the grid into a set you tick: merge them all into one, mark them
  all *Unknown*, or say none of them are people (or animals) at all. Merging a
  set that holds more than one name asks which name stays, once, rather than
  once per pair.

### Changed

- **Trove draws its own marks now.** The stand-in for a photograph it could not
  thumbnail was a full-colour platform emoji, beside a sidebar drawn in
  monochrome strokes, and it stayed light-on-light when the theme turned over.
  The rest of the chrome was typography — « » and ‹ › for going back and forth,
  × in three places and ✕ in a fourth, − and + on the map's zoom — each
  inheriting the body font, so no two shared a weight with each other or with
  anything around them. All of it is drawn now, in one family at one stroke: the
  file-type marks, the chrome, the pin on a located tile, the paw where a pet's
  crop will not load, the tick on a chosen card. The sidebar's collapse control
  was more than a mismatched glyph — it could not be reached from the keyboard
  at all, and went on offering to collapse a sidebar that was already collapsed.

- **Trove asks its own questions.** Removing an archive, rejecting a face or a
  whole selection of them, saying "not a person" or "not an animal", detaching a
  photo from someone — seven answers were taken with the browser's own confirm
  box. Every one is harder to undo than the merge that had the app's only
  designed dialog, and every one arrived as system chrome: wrong typeface, a
  generic OK, no way to mark the destructive one as destructive, and in the
  desktop app a window that visibly comes from somewhere else. They use the
  app's own dialog now, and its confirm button carries the action's words —
  *Remove archive*, *Not a person* — in the destructive colour where the action
  cannot be taken back. The two places that put a raw server error in an alert
  box are toasts, like every other failure in the app.

- **A tooltip that arrives where you are looking.** About twenty controls carried
  a native tooltip, ten of them repeating the label already under them word for
  word. The native one waits about a second, cannot take the app's theme, is
  drawn at the pointer rather than at the control, and never appears for the
  keyboard — so the one thing a bare chevron needs, a name, was the hardest
  thing to get out of it. Trove draws its own now, at the control, for the
  pointer and the keyboard both. Where a control shows its own words the tooltip
  is simply gone; the sidebar's keep theirs for when it is collapsed and the
  words are not there any more.

- **Library health reads down the column.** Six finished stages reported 136
  files, 130 copies, 12 photos, 4 photos, 2 items and 1 file, one under the
  other, which reads as a system giving up — while all six were complete and
  simply pointed at different subsets. A stage that covers part of the archive
  now says what of: "2 of 6 files indexed". Green means the stage found
  something rather than merely finished, so three stages reporting nothing no
  longer read as an archive in good shape. And only one card can be next: the
  panel had been marking every waiting card "Up next", four of them in a row on
  a full feature set.

- **A panel with nothing to say takes its space back.** People, Pets and
  Duplicates each opened with a full-width panel that, once settled, read "All
  unique photos scanned." directly under a tile reading "Scanned 4 / 4" — the
  same fact in four times the space, as the most prominent thing on a screen
  with nothing to report. A panel that is usually empty of news teaches people
  to skip it, which costs exactly on the day it has some, so it now leaves with
  its message. Duplicates also loses its second description of itself, which
  restated the line already in the page head.

- **Words for what a thing is, not how it was computed.** "Conservative visual
  grouping", "Single or uncertain sightings" and "Animal/toy overlaps filtered
  out of People" sat where a reader expects to learn what they are looking at.
  They say what is in the list now, and the third says what it is for — it is a
  review queue, and nothing on the screen had asked anyone to do anything with
  it. "The pipeline" is the scheduler's word and has left the setup screen; the
  list on Places is headed "Every place" rather than being a fourth thing called
  Places; and "items", "photos analyzed" and "organize" are files, analysed and
  organise, in an app whose prose is otherwise UK English throughout.

- **A model download says which feature it is getting ready.** A new archive's
  first minutes read "Downloading adaface model: 7% of 249 MB". An adaface is a
  file in this repository; nobody outside it knows what one is, and that was the
  first thing Trove ever said to anybody. It names the face recogniser, the pet
  recogniser, the picture-text reader and the translator now. The percentage was
  also being cut off the end of the sidebar's label — it moves to the slot that
  exists for it, and the bar stops sweeping while a real figure is showing.

- **Browse says what it is holding back.** The Overview's "All files" tile is a
  button into Browse, and the two disagree by however many duplicates you have —
  136 against 6 on our own fixture — because a redundant copy is still a
  catalogued file, while Browse deliberately shows one of each thing rather than
  the same photograph a hundred times. Nothing said so, and the gap is one press
  wide. Browse's count now carries the difference: "6 files · 130 copies
  hidden", the second half opening Duplicates, which is the screen that can do
  something about it. Storage names the redundant share of its total too, having
  quoted the size of the folder as though it were the size of the library.

- **One palette for the media types, in both themes.** The Storage bar, the
  Duplicates split and the Timeline series took one set of colours; the stat
  chips beside them took another. Six hues against four, with nothing relating
  them, and the first set was the dark-theme variant used on white as well —
  which is where the hot pink and orange bar on the light Overview came from,
  the loudest object on a screen whose accent is blue. There is one set now, at
  the lightness each theme needs, so a colour means the same thing wherever you
  meet it.

- **The search box looks like somewhere you can type.** It was transparent on the
  toolbar's own surface, so with the placeholder showing it read as a heading
  with a magnifier beside it, and the only thing in the row that looked
  interactive was the Search button. It takes a recessed field of its own now,
  and lights when focused — the one state that row had no way to show. The
  per-stage pause control, which sat at 40% opacity until you happened to hover
  it, is now the quietest thing on its card rather than invisible.

- **A filter that cannot help you looks unavailable, not empty.** The Browse
  filter row answered one situation three ways: a real disabled select for
  places, a built one for people and pets, and — where there was nothing to
  offer yet — two inert boxes that looked like empty text fields you could type
  into, with the explanation reachable only through a tooltip. All three are a
  disabled select now, whose one option says why it cannot help. Disabled
  controls are recessed and dashed rather than merely faded, so "All months"
  no longer sits between two working selects looking like a third.

- **Rename and Remove are buttons, and the map's zoom is the app's shape.** On
  the first screen anybody sees, an archive card's two actions were bare
  coloured words positioned by float, which is why Remove sat second on screen
  despite coming first in the markup. They are quiet buttons on a row of their
  own, in source order, with Remove taking the destructive colour on hover
  rather than wearing it at rest. The map's zoom, which its library draws square
  and joined ten pixels from one of the app's own pill-shaped controls, takes
  the app's shape too. And the lift a button makes under the pointer now honours
  "reduce motion", which is the one interaction on every screen and was the one
  none of the eight stylesheets that honour it had covered.

- **The features sheet's ring and its price mean something.** All eight cards
  wore the ring that means "on because you chose it", including the two that are
  on by definition, so it distinguished nothing. The footer's "715 MB to
  download" was what the current selection still owes, which beside a disabled
  Save on an untouched sheet reads as the cost of saving: a change now quotes
  what the change adds, an untouched sheet reports what the archive is already
  waiting on, and with neither it says nothing at all. Save is also no longer
  the least visible thing in the footer while it waits to be pressed.

- **Headings and labels at the size their job needs.** A stat tile's label was
  uppercase with letter-spacing in one stylesheet and sentence case in another,
  and which you got depended on nothing anyone had decided: "ALL FILES 136" was
  shouting a noun. Capitals now label a region — a table's columns, a section's
  eyebrow — and a label on a value is a sentence. Section headings were 14px
  inside a panel and 18px on People, Pets and Places with nothing choosing
  between them; "How dates were found" on the Timeline wore the 36px page title
  for a subsection; and the "/ 4" under a stat came out at 8.75px in the
  explainer behind it.

- **One back arrow, and one quiet button.** Four screens drew "go back" and drew
  it three different ways — a stroked chevron in the sidebar and on a person's
  page, a typed `←` on a pet's page and above a search ranking, which is a
  different mark at a different weight. They are now the one control a person's
  page already had: a quiet chevron, with where it goes in the tooltip rather
  than spelled out on screen. It also fits the collapsed sidebar, which used to
  hide the only way back to your archives when you narrowed it.

- **No more blue words that are really buttons.** *Set*, *Copy*, *Clear filters*,
  *Clear search*, *Put back*, *Undo*, *Remove name* and the two "open this
  elsewhere" links were plain accent text — which reads as a link, in an app
  where the only other blue words go to the documentation. They are the same
  quiet button as everything else now, just smaller where they sit beside a
  heading or inside a row.

- **The buttons that offer something all look alike again.** *How this works*,
  *Manage features*, *Select*, *Keep* under a duplicate copy and the actions on
  the selection bar are the same kind of control, and had drifted to three
  corner radii, three colours and three hovers between them. One shape now, so
  the difference between a button that offers and a button that asks — *Resume
  all*, *Save changes* — is visible again.

- **A search shows you a bit of every way it looked, and lets you open one.**
  Browse answers in up to three groups — what your filenames matched, what your
  documents said, what your pictures look like — and they were stacked in one
  scroll, each loading more of itself as you reached its end. So the documents
  group sat below every filename match there was, and moved further down with
  every one that loaded: on a search with thousands of hits the later groups
  could not be reached at all. Each group now shows its top two rows with a
  **Show all 2,847** under it; pressing that gives the screen to that ranking
  alone, with **Back** to return. Filtering and sorting leave you inside the
  ranking you are reading — only a new search returns you to the summary.

- **"Nothing found by …" now sits above your results rather than under them.**
  The line naming the ways that were searched and came back empty was printed at
  the foot of Browse, below every photo that did match — so on any search with
  more than a screenful of results, the reader wondering whether their documents
  had been searched at all had to scroll past the answer to find it. It is now
  the first thing under the search box, where it qualifies the groups that
  follow.

- **Two smaller things on the Library overview.** Each headline tile opens a
  screen, and now says so — a chevron, and a label that names where it goes
  rather than announcing a bare number to a screen reader. And **Manage
  features**, the only way from inside an archive to change what Trove does with
  it, reads as a control rather than as a footnote.

- **The Duplicates screen says what it will and will not do.** It now states
  plainly that Trove never deletes anything and that freeing the space is yours
  to do, which is what the reference page already said and the screen did not.
  The breakdown of what the copies are can be read by size or by file count, the
  way the Library overview's storage panel can — the bars were drawn by count
  alone, which cannot answer a question about disk space. The copy that Trove
  keeps is now the most prominent thing in its group rather than the faintest,
  and the same three words — kept, identical copy, visual match — are used for a
  copy wherever it appears.

  Files nothing can be drawn from now simply show their file-type icon, which
  is what it was always for.

  Videos passed over while this was broken are picked up again the next time
  the archive is opened. Both stages had written the failure down as a fact
  about the file — description search as "this video cannot be read", face and
  pet detection as "there is nobody in this video" — and neither would have
  looked again on its own.

  Five video formats were additionally never given a thumbnail even before
  that, and for longer: AVCHD camcorder clips (`.mts`, `.m2ts`), `.3g2` phone
  video, `.flv` and `.swf`. The grid marked them as video but the thumbnailer
  did not recognise them, so it tried to open them as photographs and, failing
  that, sent the whole clip to the browser as if it were the picture.

  When it does need to check, it now says "Checking for work" rather than
  "Counting files in this folder". Counting the files is how Trove works out
  whether there is anything to do; it is not a second pass over your archive,
  which is what the old wording looked like sitting on the Indexing card.

  For the same reason, indexing says "Checking 43,200 files for changes" while
  it walks files it already holds, where it used to say "Re-checking 43,200
  files already scanned". Going over known files is how an edit gets noticed —
  and how a deletion does — rather than work being done twice.

  The comparison itself was replaced too, with an index that finds exactly the
  same matches roughly twenty times faster — so even the first run after
  upgrading, which has to look at every photo once, takes about a minute
  instead of twenty. Duplicate groups are unchanged: the new code reproduces
  this archive's 18,916 groups, the same hidden copies and the same choice of
  which copy stays visible.

### Fixed

- **The Overview and Places agree about locations.** The Overview reported "With
  a location 0", and a health row reading "No locations found" with a green
  finished mark beside it, while the Places screen that tile opens reported 12
  photos in one named place. Both were counting honestly — one counted files
  carrying coordinates, the other membership of a place — and the two come apart
  the moment anyone uses the features Places exists to offer: a place pinned on
  the map, or media put in one by hand. The tile, the health row and the
  features sheet all quote the count Places itself draws now, and the tile says
  "In a place", which is what it has always been counting.

- **A narrow window keeps its navigation, and the way out of it.** Under 800px
  the sidebar becomes a horizontal bar, and the rule hiding one label there
  stopped matching anything when the appearance control moved into settings. So
  the label stayed, the row ran past the edge of the window, and app chrome
  became a strip you had to scroll sideways with "Setting" clipped at the end.
  The same block hid the archive's name and the way back to your other archives
  and put nothing in their place, which left editing the address as the only way
  out. The back control is thirty pixels wide; it stays, first in the row.

- **The Timeline's chart belongs to whichever theme you are in.** Its gridlines
  and axis text were fixed at values chosen against the dark theme, so on the
  light theme's white card they came out near-black and read as the data rather
  than as the grid behind it. With only one month to show there is no shape to
  draw at all — every series normalises to its own maximum, so a single period
  stacks them into one dot on four unlabelled rules — and the chart now steps
  aside and says the counts in words, naming the month the way the filter bar
  names it rather than as "2026-08".

- **Select shows when it is on, and is offered where it can be used.** It is a
  toggle, and it was pixel-identical in both states, so the only evidence the
  mode was on was the bar at the foot of the window — and anyone who did not
  connect the two and pressed it again to start selecting cancelled instead. It
  lights up now. On People and Pets it had also sat at full strength directly
  over "No faces yet", offering a selection mode over an empty grid; it appears
  when there is a card to select, and goes away with the last one.

- **Links stopped drawing themselves as selected.** A stray comma in the
  stylesheet handed every link in the app the tint that marks selected text. It
  showed on the reference pages, where "On this page" is a column of them: all
  seven entries read as selected at once, which left the mark showing the
  section you are actually in with nothing to say.

- **An empty screen's sentence is laid out as a sentence.** "No repeated pets
  grouped yet." was appended straight into the card grid, so it became one
  120px cell and broke after three words inside a container a thousand pixels
  wide. It spans the row now, as does the one below it. Section titles on
  People, Pets and Places also sat two pixels to the right of everything under
  them.

- **"Where Trove looks when you type" stops reporting the filter bar.** The
  filename row answers how much of the archive that way can reach, but read the
  count after Browse's filters — so narrowing the grid to videos made the panel
  claim filename search could reach three files. It reaches all of them,
  whatever the filter says. Nothing signalled the mistake, because the two rows
  under it come from elsewhere and correctly did not move. The panel also says
  how much each way has *not* covered yet: it had been reporting "1 document"
  and "1 video · 1 image" with no hint that most of what each was pointed at was
  still outside.

- **The start page holds its shape while it loads.** The one screen every user
  sees first was the only one in the app with no loading state: between paint
  and the answer it showed "Your archives" over an empty region with the
  three-step guide collapsed against it, which reads as an app that has
  forgotten the folders you added. It holds a card's worth of space per archive
  now, counted from the number it saw last time, so the row does not jump when
  the answer lands.

- **"All results" stops throwing you back to the top.** Widening a description
  search from its top matches to everything only adds results *below* the ones
  already on screen — it is the same ranking in the same order with the
  relevance floor taken off — so being scrolled to the top for it was the screen
  answering "show me more" by taking away what you had. Coming back out of one
  ranking to the overview also holds your place more reliably: it now waits for
  the results it re-fetched instead of putting you back a frame before they
  arrive.

- **The drawer of set-aside groups is called "Unknown", not "Hidden".** It holds
  exactly the groups you marked *Unknown person* or *Unknown animal*, and named
  itself after what happened to them rather than after what you said about them.
  ("Not a person" and "Not an animal" never appear there — those leave grouping
  altogether and have no group left to put back.)

- **A feature card in Manage features is as big as what it has to say.** Resting
  on one showed its description in a panel two-thirds larger than the card,
  hanging over the row beneath and covering the cards there. The cards are now
  as tall as the longest description among them, and all the same height, so
  turning one over changes nothing but that card.

- **The Timeline notices the people you have just named.** Naming people and
  stepping over to the Timeline still offered the list of names from before you
  named them, while Browse's identical filter was correct — the Timeline keeps
  its chart and its place while you are away, and was keeping its stale list of
  people along with them. It now asks again on the way back, holding whatever
  you had narrowed it to.

- **A name with a quotation mark in it can still be changed.** On a person's
  own page the rename button was built with their name written into its click
  handler, escaped for the JavaScript inside it but not for the HTML around it.
  A name like `Ana "Nana"` ended the attribute early, so the button rendered,
  looked right, and did nothing at all — with no way back, since renaming was
  the thing it did.

- **A person, pet or place counts its files, not its photographs.** The number
  under a name has always been a count of distinct files, and a face is found in
  a video as readily as in a photograph — so a group holding two clips called
  eleven of them "11 photos", which is wrong about what it holds and about what
  opening it will show you. Every place that prints it — the card, the card
  while you are renaming it, the group's own page and the toast after a merge —
  now says "11 files".

- **Collapsing the sidebar no longer puts a scrollbar down its side.** The
  Settings button kept its full-width label in a rail two-thirds narrower than
  the label, so the sidebar overflowed and grew a horizontal scrollbar on every
  screen. It now shows the gear alone, as every other item in the rail does.

- **"Merge with…" can be scrolled to the name you want.** The list shut itself
  the moment you tried to scroll it, so on an archive with more than a handful
  of named groups only the first few were ever reachable. The menu is pinned to
  its card and closes when the screen scrolls out from under it, and that was
  reading a scroll of the list itself as a scroll of the screen.

- **A group's name and its ⋯ menu answer the first press again.** Pressing the
  name of a person, pet or place on its card, or the ⋯ beside it, often did
  nothing at all: the card is draggable so that one can be dropped onto another
  to merge them, and a press that travelled the few pixels any real click
  travels was read as the start of that drag instead. No click was ever
  delivered, so the rename editor never opened — which is what "renaming
  doesn't always work" was, and it was never the saving. A press that starts on
  one of the card's own controls no longer arms the drag; a press anywhere else
  on the card still does.

- **Opening a group no longer loses your place in the grid.** Scrolling through
  People or Pets, opening someone, and coming back put you at the top again —
  and threw away every page the grid had loaded on the way down, which on a
  screen of several hundred groups is most of what you were looking at. The
  grid is now set aside while a group's page is open and put back as it was,
  reconciled against anything you changed while you were in there.

- **Merging no longer throws away your place.** Merging two pets rebuilt the
  entire Pets screen, and merging two people rebuilt the grid from its first
  page — so the scroll position and every page loaded into it went, once per
  merge, on the screens where merging is something you do dozens of times in a
  row. Both now patch the cards that changed and leave the rest alone, which is
  what the background refresh has always done while a scan is running.

- **A name you take off a person stays off.** Clearing the name worked and then
  quietly undid itself: any face you had moved onto that person by hand still
  carried the name, those moves are remembered *by* name, and the next
  clustering pass read one as an instruction and rebuilt the person — same
  name, new group. Clearing a name now releases those faces too, and the
  editor says "Remove name" rather than leaving you to discover that an empty
  field means the same thing. Undoing it from the history puts both back.

- **Pets can be renamed again.** Neither way of naming a pet worked. On the
  Pets grid the name looked editable — it highlights under the pointer, like a
  person's does — but nothing was listening to it; the control had never been
  built. On a pet's own page the name opened a system text box, which the
  desktop window does not provide, so clicking it did nothing at all. Both now
  do what the People screen has always done: the name becomes a field where it
  sits, and Enter or clicking away saves it.

- **People stop vanishing from photos they are plainly in.** The face quality
  gate discarded the bottom tenth of every archive by construction: the rule was
  a cut on a score measured against that archive's own mean and spread, so a
  fixed share was always thrown away, however good the photographs were. On one
  100,000-file archive that was 2,804 faces — sharp, well-lit, frontal portraits
  among them, and on 754 photos the only face found, which then reported no
  people at all. Whether a face can *start* a person is still judged against the
  archive; whether it is unusable is now judged on the face itself, against
  `faces_fiqa_floor_norm`. The same archive discards 2.6% instead of 10.5%.
  Nothing was ever deleted, so `trove faces --recalibrate-fiqa --recluster` puts
  the recovered faces back into people without re-reading a single image.

- **An archive whose folder cannot be found says so.** Disconnect the drive an
  archive lives on — or move or rename its folder — and the Library overview
  reported perfect health: green dots, a full file count, "Up to date" in the
  sidebar, while every thumbnail and original in it failed to open. The start
  page had been marking the same archive "not mounted" the whole time. The
  health panel now leads with what has happened, names the folder, and says what
  to do about it; the sidebar says it from every screen; and the headline
  figures stop presenting themselves as current, because what they describe is
  the catalogue rather than files anything can read.

- **Pausing one step is no longer reported as pausing everything.** The sidebar
  said a flat "Paused" over an archive that was busily indexing, next to a
  button still offering to "Pause all" — or, if the stopped step happened to
  have nothing waiting, "Up to date" over a step that would silently ignore the
  next thousand photos. It now says how many steps are stopped, and never
  disagrees with the button beside it.

- **A paused step keeps saying how much is waiting.** It reported the bare word
  "Paused" and dropped the backlog — the one number the person who paused it
  comes back for. It now reads "Paused · 25 waiting", and a step paused with
  nothing outstanding no longer prints a "finished" line under a stopped dot.

- **A figure produced by a paused step no longer looks current.** With duplicate
  detection paused, "Redundant copies" read 92 while the archive held 112, in
  the same type as the live figures either side of it. Such a tile is now dimmed
  and says which step is stopped.

- **The headline row fills the width again.** It was fixed at four columns, so
  any archive not running Places — three tiles — left the row ending well short
  of everything below it.

- **An empty archive reads as empty**, rather than as a storage chart that
  failed to load: the blank bar and the table header with no rows under it are
  replaced by a line saying whether Trove is still reading the folder or has
  been through it and found nothing.

- **The storage table says what its percentage is a share of.** The Size/Files
  switch changed what the "Share" column divided by without changing the column,
  so with Files selected a row read "304 · 17.9 MB · 93.3%" and the percentage
  attached itself to the megabytes beside it.

- **The Duplicates screen keeps up with the grouping it is reporting on.**
  Duplicate detection is scheduled rather than started by hand, so the screen is
  routinely opened while it is still running — and once opened, it stayed frozen
  at whatever it had found in that first moment for the rest of the session. It
  would read "0 duplicate groups" and "no copies found yet" while the sidebar
  said "Up to date" and the Library overview reported ninety-one redundant
  copies, and leaving the screen and coming back showed the same stale figures
  again, because returning to a screen replayed it rather than asking. The
  figures now move as the run goes, the list fills itself in when the run
  settles, and coming back to the screen brings it up to date.

- **Copies in a group say which file they are.** A group was drawn as a row of
  thumbnails of the same picture over a folder path cut off at whatever fitted,
  which in a deep archive meant nine copies all captioned `Takeout/Google
  Photos/…` and no way to tell one from another without opening each in turn.
  Every copy now carries its own file name, and the folder is shortened to the
  part that actually differs — `…/Bariloche - dia 1` beside `…/Bariloche - dia
  2` — with the whole path still one hover away.

- **A folder with a quotation mark in its name no longer breaks its tile.**
  `Fotos de "Mama" & Papa` — an ordinary thing to find in a Takeout export —
  ended the tile's tooltip early, spilled the rest of the name into the page as
  visible text, and destroyed the tile's own click handler, so that one copy was
  the single file in the archive that could not be opened.

- **The Duplicates screen can be used from the keyboard.** Its copies were the
  one grid in the app not built as real buttons: a hundred and fifty pictures on
  a page that no keyboard could reach and no screen reader could name. They now
  behave like every other tile in Trove, are announced with the file's name and
  where it lives, and take a visible focus ring.

- **Filter controls are legible in the light theme.** A rule meant for the dark
  panel inside the viewer was reaching every filter and sort control in the app,
  painting white text on a white background — both of the Duplicates screen's
  controls, and the Timeline's, were invisible until clicked.

- **A duplicated PDF shows its first page**, the way it already did everywhere
  else in Trove, instead of a generic document icon; and a duplicated video is
  marked as a video rather than looking like a photograph.

- **Changing the duplicates filter returns you to the top of the list**, instead
  of leaving you somewhere in the middle of a list you had not seen the start
  of, with the control you just used scrolled off the screen.

- **An archive id that does not exist is answered as such.** A stale bookmark,
  or a tab left open while another window removed the archive, produced SQLite's
  own words — "unable to open database file" — which reads like a broken disk
  rather than a request for something that is not there.

- **Thumbnails are not fetched again every time you look at them.** Nothing
  Trove sent the browser said how long it could be kept or how to tell whether
  it had changed, so every screen change re-downloaded the whole grid and every
  arrow press in the viewer re-downloaded the whole filmstrip — and each of
  those requests rebuilt the picture from the original file if it was not
  already on disk. Grids paint quickly on a second visit now, and moving
  between screens no longer competes with indexing for the disk.

- **A grid full of thumbnails no longer stalls because of one file in it.**
  When Trove could not draw a thumbnail for something, it sent the browser the
  whole file instead and let it try — so a tile for a spreadsheet quietly
  downloaded the spreadsheet, one for a camera RAW downloaded 25 MB, and the
  viewer, which fetches a thumbnail for every file on either side of the one
  you opened, could start sending a multi-gigabyte backup archive to draw a
  62-pixel square. A browser only fetches a handful of pictures at a time, so
  every other tile on the screen waited behind that, and the ones that gave up
  waiting stayed blank until you navigated away and back.

- **A photo missing its last few bytes gets a thumbnail like any other.**
  Photographs arrive truncated more often than you would think — an
  interrupted copy, a phone unplugged mid-transfer, a Takeout export that lost
  its tail. Trove refused to read them, where every browser draws them without
  complaint, so those tiles fell back to downloading the whole original.

- **Videos have their thumbnails back, and are searchable by description
  again.** Since the previous release nothing could be got out of a video at
  all: the grid showed a bare film icon for every clip, no face or pet was
  ever found in one, and description search quietly stopped indexing them.
  The frame extractor was writing to a scratch filename ending `.tmp`, and
  ffmpeg picks the format to write from the filename — it recognised nothing
  and refused each job before reading a frame, without saying so.

- **"Counting files in this folder" no longer sits on the Indexing card most
  of the time.** Every time the Trove window came back to the front, it threw
  away its record of how many files were in the archive folder and re-counted
  from scratch — replacing "97,083 files catalogued" with a progress message
  for the twenty seconds that takes, on a large archive. Switch to another app
  and back a few times and it was rarely off the screen. It now keeps the last
  count and refreshes it in the background, so the card keeps its answer.
  Changes are still picked up exactly as quickly.

- **Finding duplicates no longer takes twenty minutes every time you touch the
  archive.** On a 97,000-file archive a duplicate rebuild took 19½ minutes, and
  it ran again in full after any scan — including one that found nothing but
  five deleted files, none of which had duplicates at all. Nearly all of that
  time went on rediscovering which photos look alike, which Trove now writes
  down against the exact content it found them for. Add twenty photos and only
  those twenty are compared; delete some and nothing is. The same rebuild is
  now **3 seconds** on an unchanged archive, and 5 on the deletion case.

- **Closing the app during a duplicate rebuild no longer hangs it.** The
  rebuild ignored the request to stop and ran to the end — with its progress
  bar frozen the whole time, so it looked like it had hung — and then threw
  away everything it had done, starting from zero on the next launch. It now
  stops when asked, and keeps the work it has finished.

- Duplicate detection could fail outright on very large archives, on some
  builds of SQLite, once an archive had more duplicate groups than the database
  would accept variables in one statement.

### Internal

- The bundled FFmpeg moves from 7.1 to 8.1. The builds Trove takes it from are
  deleted upstream after a few weeks, and 7.1 had gone from every one of them,
  so this is the version that still exists rather than a version anybody chose.
  It adds about 10 MB to each installer. Nothing Trove asks FFmpeg to do changed
  between the two.

- The stylesheets carried fourteen font weights, five colour literals naming
  states the token set already had, and a `theme.css` whose descriptions of the
  sidebar, the tiles, the filter selects and the viewer had every one been
  overridden by a file loading after it. The weights are a ramp of five, the
  colours are tokens, and the dead rules are gone — checked by screenshotting
  six screens before and after rather than by reading. `theme.css` now says at
  the top that a value in it is not evidence of what is on screen.

- A video frame ffmpeg refuses to produce is written to the log with its exit
  status and ffmpeg's own reason, instead of leaving no trace.

- A file the image decoder cannot read is logged as one line rather than a
  traceback. An archive holds plenty of files that are not pictures and every
  screen asks for a thumbnail of what it shows, so this is an ordinary
  outcome; three days of one real log held 781 of them. Anything that is not
  the decoder refusing the file keeps its traceback.

## [0.2.1] - 2026-08-08

### Fixed

- **The installed app does its work again.** In 0.2.0 an installed Trove could
  scan nothing, index nothing and download no models: it opened an archive,
  showed it, and then quietly did no work at all, on any archive, for any
  feature. One small file describing the models — not the models themselves —
  was left out of the installer when they stopped being bundled, and the first
  thing the scheduler does each round is ask that file whether anything needs
  downloading. The question raised instead of answering, and the rest of the
  round never ran.

  It also cost Spanish searches their results. Typing *bosque* looks inside the
  same English translation *forest* would, and the translator is one of the
  things the missing file describes, so it could neither be downloaded nor
  served: the search ran on the Spanish words, which — as the 0.2.0 notes
  explain — is the case that returns screenshots of Spanish text instead of
  photographs. Searching in Spanish is as good as searching in English again.

  Nothing needs reinstalling beyond the new version, and no archive needs
  rebuilding: work that never happened simply starts happening, and an archive
  part-way through picks up where it stopped.
- **A translator that half-downloaded no longer stalls every search.** The
  search box asked one of the four files whether translation was available here,
  so an interrupted download could answer yes on behalf of files that had not
  arrived — and every search then waited fifteen seconds for them before showing
  anything. An incomplete set now reports itself as what it is, which costs a
  Spanish query its translation and no time at all.

## [0.2.0] - 2026-08-08

### Added

- **Trove can read the writing in your pictures.** Switch on *Search by picture
  text* and photographed receipts, screenshots and scanned paperwork become as
  searchable as anything else. A PDF is decided page by page, so a contract with
  a scanned appendix is read both ways and comes back as one document, with the
  page each passage came from. Spanish and English, accents included.

  It downloads 30 MB the first time you switch it on, once per computer rather
  than once per archive.

  **It is the slow one, and worth deciding about rather than switching on by
  reflex.** Every picture has to be opened and looked at — it cannot use the
  small thumbnails, because writing disappears at that size — which is roughly
  half a second each. Five thousand pictures is under an hour; a hundred
  thousand is an overnight run. It runs alongside everything else, never blocks
  browsing, and stops and resumes safely, so leaving it overnight is the
  intended way to use it. A result read from a picture is marked as such,
  because unlike a document's own text it is a best guess.
- **Trove can read what is inside your documents.** Switch on *Search by document
  text* when you set up an archive, and the search box looks inside the files rather
  than only at their names: type a phrase from a contract and the contract comes
  back, showing the passage that matched and the page it was on. It reads PDFs
  that carry a text layer, Word, Excel and PowerPoint files, OpenDocument files,
  plain text, Markdown, CSV, web pages and notebooks — spreadsheet numbers
  included, since an invoice total is exactly the sort of thing worth searching
  for. Accents do not have to match (`peticion` finds `petición`), and a plural
  finds its singular.

  Nothing is downloaded for it and nothing leaves the machine: reading a
  document is parsing rather than recognition, so there is no model involved. On
  a few thousand files it is minutes, and it runs alongside everything else.

  Two limits. A scanned PDF is a picture of a page with no text in the file at
  all, so this finds nothing in one — the feature's card says so before you
  switch it on, that is what *Search by picture text* is for, and switching it on
  later re-reads every file this feature had to pass over. And pre-2007 `.doc`,
  `.xls` and `.ppt` files cannot be read at all; they are listed and dated like
  any other file and reported as an unsupported format, rather than half-read
  into something misleading.
- **Browse says what it can search, before you ask it to.** With the box empty
  there is now a panel under it listing every way this archive can answer a
  query, what each one matches in plain words, and how much of the archive it
  currently covers — so you can see that 300 documents have been read and 6,000
  photos are still queued. Each way links to the page documenting it, and the
  one fed by two or three features links to each of them. An archive with no
  search features switched on gets the panel too, saying it has one way.

  Every way is named after the feature you switched on to get it, so a group of
  results is headed with the same words as the card you chose it from, the card
  reporting its progress and the page explaining it — *Search by document or
  picture text*, *Search by description*. The one way no feature produces, searching
  filenames, is named to read alongside them.

  When you search, those same rows become the headings over their own results,
  so what the screen promised and what it labels are the same list. A way that
  found nothing says so on one quiet line at the foot rather than leaving you to
  wonder whether it ran.
- **Every archive can be searched by file name, and now it always is.** Matching
  what you type against the names of the files themselves needs no model, no
  download and no indexing, so it runs on every search whatever else is switched
  on, and gets a group of its own with the matched part of the name marked.
  Every word has to appear in the name, in any order, so `escritura 2019` finds
  `2019-escritura-casa.pdf`, and only the file's own name counts — a folder
  called `playa` does not make everything inside it a match for "playa". The
  extension is part of the name, so `.pdf` on its own is every PDF you have, and
  `escritura .pdf` narrows to the PDFs among the matches.

  Until now it was a *fallback*, reached only by archives with no other way to
  search, which meant switching on Search by description quietly took it away:
  `IMG_2019` went to the picture model, scored below its relevance floor, and
  came back with nothing at all. Searching by description no longer costs you
  searching by name.
- **A search result says how it was found.** Where a group could have found
  something two ways, each result now carries it: text read from a file's own
  words is told apart from text read off the pixels of a scan or a screenshot,
  because the second is a best guess where the first is what the file says.
- **Browse captions each thumbnail with the file's name** rather than its date.
  The grid is already broken into dated sections, so the date under every tile
  was repeating the heading above it, while the name is the one thing that says
  which file you are looking at. The grids that have no such headings — a
  person's photos, a pet's, a place's — still caption with the date.
- **Each archive now chooses what Trove does with it.** Adding a folder opens a
  setup screen: name the archive, and build up what it runs by dragging features
  onto it — People, Pets, Places, Search by description — or leave them off.
  Indexing and duplicates always run, because everything else reads what they
  produce. A feature you do not choose is not merely hidden: its stage never
  runs, and its models are never downloaded, so an archive that only wants
  duplicates found no longer waits on a 689 MB download for a search it will
  never use. Each feature says up front what it does and what it costs, and
  a feature whose models are already on this machine from another archive says
  so rather than quoting a download that will not happen.
- Features can be changed later from the same screen — "Set up" on any archive
  on the start page. Adding one picks up from where the catalogue already is;
  removing one deletes nothing, so putting it back does not start over.
- People and Pets can now be chosen independently. They still share a single
  pass over each photo, so having both costs barely more than having one.

- Semantic (description) search now runs entirely on-device on SigLIP 2, replacing
  the Voyage cloud API. Search queries and photo contents no longer leave the
  machine at all; the API-key setup step and its "no key configured" state are
  gone.
- Each pipeline stage in Library health can now be paused and resumed on its own,
  instead of only the whole pipeline at once. A stage paused this way stays paused
  across a restart — for that archive alone.
- The map has a Places/Photos switch: alongside the existing grouped-place view,
  it can plot one dot per geotagged photo, coloured by the place it belongs to, for
  seeing the actual spread of a place rather than just where you keep going back.
- **Open a file and you can see its copies.** The viewer's panel has a
  Duplicates section showing the whole group the file belongs to — every copy as
  a thumbnail, which one Trove keeps, which are byte-identical and which only
  look the same, and which one you are looking at. Pressing another copy opens
  it, with the arrows kept inside the group and a way back to where you started.
  A file that has been compared and matched nothing says so; one the last
  grouping run has not reached yet says that instead, rather than claiming it has
  no copies. This replaces a line in Details reading "3 copies", which told you
  three files somewhere were the same and left you to go and find them.
- The Duplicates page breaks the redundant-copies total down by what the copies
  actually are: byte-identical versus only visually the same, and photos versus
  videos.
- A duplicate group's copies now wrap onto as many lines as they need instead of
  scrolling sideways, so a group of twenty can actually be compared against
  itself.
- The Duplicates page can be narrowed to groups holding an identical copy or a
  visual match, and ordered by how many copies a group holds as well as by how
  much space it would give back.
- The Duplicates page leads with how many unique files the archive holds — every
  file, with each set of copies counted once — and says how much of it is still
  waiting to be compared, the same way People and Pets report their own progress.
  An archive with nothing grouped yet keeps those numbers on screen instead of
  replacing the page with an empty-state box.
- `trove logs` prints the application log, or `trove logs --path` its location, so a
  bug report can carry the evidence needed to act on it.

### Changed

- **The installer is about 46 MB smaller**, and no feature you leave switched
  off costs you anything to have installed. Three things were travelling inside
  the download that did not need to: the models that read writing in pictures,
  the Spanish translator the search box uses before a description search, and a
  library nothing ever called. The first two are now fetched the first time you
  switch their feature on — the translator arrives with the search model it
  belongs to, so nothing new appears on screen — and the third is simply gone.

### Fixed

- **The start page no longer keeps you waiting for its cards.** Opening the app
  drew everything but the archives instantly, and then sat there — three
  quarters of a second on a 97,000-file archive, longer on a slow disk. Working
  out one number for each card, the total size of the folder, meant reading the
  whole catalogue off disk; and a second number was being worked out beside it
  that nothing has ever displayed. The unused one is gone and the size is now
  looked up rather than recounted, which takes the same page from 738 ms to
  21 ms. Your archives pick this up the first time each one is opened.
- **Animated pictures are no longer grouped as copies of each other.** A GIF was
  compared on its first frame alone, so two unrelated animations that open the
  same way — a shared title card, a fade in from white — were treated as one
  picture and one of them was hidden from your library. Animations are now
  matched on their contents like videos are, and this is decided by looking
  inside the file, so a GIF saved as a `.png` is handled correctly too. Anything
  wrongly hidden comes back at the next automatic rebuild.
- **Thumbnails no longer come up blank until you force a reload.** A thumbnail
  is generated the first time it is asked for, and a second request arriving
  while the first was still writing it was answered with the half-written file —
  which the browser then remembered as a broken picture. Grids of copies were
  worst hit, since identical files share one thumbnail and ask for it at the same
  moment. Every generated thumbnail, video frame and face crop is now published
  in one step, so it is either absent or whole and never something in between.
- **A photograph found by its writing no longer swallows the results beside
  it.** Text-search results hold two kinds of file — documents read from their
  own text, and pictures read off the pixels — and a picture's thumbnail had
  nothing bounding its height there. It grew to fill the column, stretched the
  whole row to match, and left every document next to it in a cell four times
  the height of the single line of text inside it. Every result in that group
  now shows itself at the same size, whichever kind of file it is, and its name
  sits above the passage rather than under a shadow meant for laying text over a
  photo.
- **An archive that only reads the writing in pictures can now search it.**
  Switching on *Search by picture text* without its document half filled the index
  exactly as it should, and Browse never showed the group that searches it — so the work
  was done, the passages were there, and there was nowhere to look. The same
  archive was also told it had nothing to read and nothing queued, because
  coverage counted documents rather than the files its readers actually open.
  Both halves write into one index, so either one alone now brings the group and
  the count with it.
- **Browse's "How this works" opened the page about scanning folders**, which
  answered a question nobody pressing it had. It now opens a page about
  searching — and both text features have one to link to from the setup screen,
  which they did not before, so choosing them was the one decision
  made with no way to read what it does first.
- **Browse opens on a finished-looking screen, and opens far faster.** It used
  to wait for the filter bar's options — which years, which people, which
  places this archive holds — before doing anything else, and that answer is a
  pass over every file in the catalogue. So the screen you got first was a
  heading, an empty toolbar row with a stray sort box adrift at its end, and no
  media at all; on a large archive just after opening it, that sat there for
  several seconds, because nothing it needed was in memory yet and the pipeline
  was busy with the same disk. Now the media grid loads at the same time rather
  than behind it, the sort control arrives filled in, and the filters stand in
  for themselves at their settled size until they land — nothing on the screen
  moves when they do. Coming back to Browse reuses the options it already
  fetched, so the bar is simply there. The queries behind all of this were
  narrowed too: on a 97,000-file archive with nothing cached, the filter
  options went from 2.4s to 0.2s and the first page of media from 0.74s to
  0.35s. Existing archives pick that up the next time they are opened.

- **A paused scan no longer looks like it threw away its progress.** Resuming
  sent the bar back to 0 and raced it up to where it stopped, which read as the
  whole scan being redone. Nothing was: scanning restarts at the top of the
  folder rather than at its own backlog, and a file it already has costs a
  quick check and no re-reading, so it crosses that ground very fast. It now
  says what it is doing — "Re-checking 12,400 files already scanned…" — and the
  bar reappears at the point the interrupted run reached. Dating files had the
  same problem from the other end: its bar measured the work that was *left*,
  so each run restarted at 0% of a total that had shrunk to match. Both now
  measure against the whole archive, the way finding people and indexing for
  search already did.

- An archive set up **without** Search by description was still shown the
  description-search box at the top of its browse screen, above a line
  promising files "queued for indexing". Nothing was queued and nothing ever
  would be — declining the feature leaves its indexing stage out of the
  pipeline entirely — so the search could only ever come back empty. The box
  and that line now appear only on archives that run the feature; the grid,
  filters and sorting are unaffected.

### Changed

- **Files you add to an archive are picked up in seconds, not minutes.** Trove
  used to find them only on its next sweep, and those sweeps get further apart
  the longer an archive sits quiet — up to five minutes — so dropping a folder of
  photos in and switching back to Trove meant watching nothing happen for a
  while. It now watches the archive folder and starts within a couple of seconds,
  and checks immediately whenever you switch back to the window. Neither replaces
  the old sweep, which still runs: filesystem notifications are not delivered at
  all on some setups, network drives especially, and there the sweep is still
  what finds your files.
- **A file still being copied is no longer read half-finished.** A large video
  dropped into an archive could be catalogued mid-copy — the wrong size, and a
  thumbnail, date and search index all built from the fragment, thrown away and
  redone once the copy finished. Trove now leaves a file that is still arriving
  and comes back to it, so what gets read is the whole file. Photos are unaffected
  in practice: they land in one go, and the check costs them nothing. The scan
  summary says how many files it is still waiting on.
- **A folder on the start page says "so far" while it is still being read.** The
  file count and size on each card are what Trove has catalogued, so during a
  first scan they are a number on its way up — and quoted plainly, they looked
  like the size of the folder. They now read *12,040 files so far* until a scan
  has been all the way through the folder, and drop the qualifier once one has.
  An archive that has never finished a scan keeps it, as does one whose last scan
  had to leave a file that was still being copied — in both cases the card really
  is short of what is in the folder.
- **The two text features are named after what you can search, not the files they
  read.** *Documents* and *Pictures of text* are now **Search by document text**
  and **Search by picture text**, headed as *Search by text extracted*
  where both are on. The old names described their input, which read as a promise
  about a kind of file rather than a way of finding one — and *Documents* was
  actively misleading, since a scanned contract is a document and is precisely
  what that half cannot read. Both now sit in one grammar with *Search by
  description* and *Search by filename*: every way of searching is named for what
  you type against. Nothing about what they do has changed, and an archive already
  running them keeps everything it has read.
- **The command is now `trove`, not `oa`.** The rename to Trove previously
  stopped at what you could see; the package, the command and the data folder
  were all still called `organize_archive` underneath. They are not any more —
  there is one name for all of it. Your catalogue moves with it: the first time
  this version starts it relocates the old data folder and repoints what is
  recorded inside it, so nothing is re-scanned and nothing is lost. If you have
  scripts, aliases or shell shortcuts that call `oa`, they need updating to
  `trove` — the old command is gone rather than kept as an alias, so they will
  fail loudly rather than quietly doing something else. `OA_LOG_LEVEL` is now
  `TROVE_LOG_LEVEL` for the same reason. On Windows, because the application's
  installer identity changed too, an install of an older version is not
  recognised as the same application and is not replaced by the upgrade; it can
  be uninstalled from Add/Remove Programs once the new one is running. Your
  catalogue is untouched by that — it lives in the data folder, which is
  migrated.
- **Library health now draws the pipeline as the chain it is.** The five status
  cards were a grid of equal tiles, which said the archive does five unrelated
  things at once; the one real relation between them — Indexing and Duplicates
  run first and everything else reads what they produce — was left to the words
  "Waiting for Duplicates…" inside a tile. They are now rows on a rail, with the
  two that always run marked as such and the rest hanging off them: the same
  chain the setup screen draws before any of it starts. Each row is full width,
  so it says more in less height than the tile it replaces.

- **The Library section is now called Browse.** Every other section is named
  for what it holds — People, Places, Duplicates — and this one is named for
  what you do there: look through the whole archive, by filter or by
  description. "Library" still means the collection itself, as in Library
  health on the Overview.

- **A feature is called the same thing everywhere, and carries the same mark.**
  The setup screen offered "Search by description"; Library health then reported
  on it as "Semantic indexing" and the sidebar announced it as "Indexing search…",
  and nothing on any of those screens said they were the same thing. Every card,
  chip and status line now takes its name from the feature you chose, and each
  feature has one icon — on the card you press, on the link in the pipeline, on
  the card reporting its progress, and on the section it unlocks. The shared
  People & pets card also stops naming the half you did not ask for: on a
  pets-only archive it says "Finding pets…" rather than promising people it was
  never going to look for.

- **Model weights are downloaded when you create the archive, not hours later.**
  The setup screen quotes what a feature costs to download, and the download then
  waited for the stage that needed it — which waits for scanning, dating and
  duplicate-finding to finish first. On a large folder that meant choosing Search
  by description in the morning and a 689 MB fetch quietly beginning in the
  afternoon, long after anyone was watching for it. It now starts as soon as the
  archive is created, runs alongside the first scan, and is usually finished
  before the stage that needs it is reachable. The sidebar shows what is
  downloading and how far along it is; features you did not choose are still
  never fetched, and an archive whose models are already here from another
  archive downloads nothing at all.

- **Search by description returns more of what you asked for, and less of what you
  didn't.** Two things were wrong with how weak matches were hidden. The scores
  themselves were squeezed into a narrow band, because the model places pictures
  and sentences in two separate regions of its vector space; measuring each from
  its own centre spreads them out roughly threefold. And of the two thresholds
  that decide what to show, one sat below every score it could ever see, leaving
  the other to do both jobs — the only way it could suppress a bad result was to
  tighten until good ones fell out too. On a test archive a search for "a lake"
  returned 7 of its 15 lake photos; it now returns them all, while a search for
  something the archive does not contain returns nearly nothing instead of a
  confident-looking page. Nothing is re-indexed.
- **The installers are roughly half the size.** Three things were shipping weight
  nobody could use: the People and Pets model weights travelled inside the download
  (349 MB) even though the app already knows how to fetch weights on demand; FFmpeg
  was bundled as two self-contained binaries that each carried a complete copy of
  the codec set; and the OpenCV build shipped a full Qt desktop toolkit for an app
  that never opens a window. The weights are now downloaded on first use like every
  other model, so the first People/Pets run fetches about 550 MB instead of 220 MB
  and needs a working connection once. Nothing else about the app changes.
- Face clustering no longer needs the `faiss-cpu` package. The search it did is
  now done directly in NumPy, producing identical people and identical groupings —
  a clustering pass is 1.3–2.3x slower, which is a few minutes against a detect run
  measured in hours, and the wheel was 62 MB of installer.
- Model downloads now report progress. A first run that fetches several hundred
  megabytes used to show one unchanging line on the stage card and read as hung.
- The Overview storage panel was reworked: one bar with a Size/Files switch
  instead of two competing bars, exact numbers moved into the table, and per-type
  share shown on hover. Either end of that switch flips it, rather than the lit
  end doing nothing.
- A stage card no longer shows a progress bar while the stage is still setting
  itself up. Counting a 150k-file folder or fetching model weights happens before
  a single file is processed, and a bar sitting at 0% across it read as a run that
  had hung; the card now says what it is preparing, and a model download's own
  percentage is the headline rather than a footnote beside the bar.
- The Overview's "All media" tile is called **All files**: it counts everything
  catalogued, documents and archives included, not just photos and video.
- Merging two people of the same size, neither of them named, now keeps the older
  of the two rather than depending on which card you dragged onto which — the rule
  pets and places already followed.

### Fixed

- The archive setup screen no longer carries a name between folders. Naming one
  archive and then adding a second offered the first one's name in the field,
  because the panel is hidden rather than rebuilt when it closes and the name was
  read back off it. A name typed while adding a feature is no longer lost either,
  in either direction: half-typed names now survive the panel re-rendering, and
  renaming an existing archive survives it too instead of reverting.
- The blinking cursor in the "Search by description" card on that screen is now
  still, like every other card's picture.

- Pausing responds immediately. A pause asks the running job to stop at its next
  batch checkpoint, which takes seconds, and the card went on reporting the work
  as if nothing had happened before stopping abruptly; it now says "Pausing…" the
  moment you press it. A stage stopped part-way also keeps the progress bar it
  reached, since the run is suspended rather than thrown away.
- Pausing no longer follows you to another archive. The pause was one app-wide
  switch, so stopping work on one folder left the next folder you opened stopped
  too, with nothing running and a Resume button nobody had pressed. Each archive
  now remembers its own pause, whole-pipeline and per-stage.

- Seeking in a video or audio file is reliable. The server mishandled two of the
  requests a player makes: asking for a position past the end of the file produced
  a malformed reply instead of a "that range does not exist" answer, and asking for
  the *last* few bytes — how a player finds the index in an MP4 that stores it at
  the end — returned bytes from the *start* of the file while labelling them as the
  end. Depending on the player, that showed up as a clip that would not scrub, or
  one that refused to open at all.

- People & pets detection no longer stops with "neither the face nor the pet models
  could be loaded" when the app is run from a source checkout. Two of its four model
  files had no way to be downloaded outside a packaged build, so the stage fetched
  ~310 MB of the other two and then failed; they are fetched and hash-verified like
  every other weight now. When a model genuinely cannot be obtained, the stage says
  so before downloading anything, and the card names the cause instead of pointing
  at messages the user cannot see.
- The Semantic indexing card reports its model download instead of sitting blank
  through it. The progress callback was captured by whichever part of the app
  loaded the model first, which was always the silent start-up warm-up. That
  warm-up no longer downloads anything either: it warms weights that are already on
  disk, so a 317 MB download can never start unannounced in the background.
- Faces that detection discards as an animal's own can be reviewed again. Since the
  people-and-pets detectors were merged into one pass, every such face was dropped
  with no record, so the "not an animal" review list on the Pets screen was always
  empty and a person mistaken for a pet could not be recovered. Correcting one now
  also survives a re-scan.
- Closing the app no longer hangs for several seconds when detection or semantic
  indexing has just started. Those stages spend their first seconds loading a
  model, which cannot be interrupted, so shutdown no longer waits on them.
- Opening the map on a freshly scanned archive could fail with a database error
  while a scan was running, because the first view clustered the places itself.
  Clustering now belongs solely to the pipeline stage; until it runs, the map
  shows the photos as ungrouped dots.
- `trove scan` crashed immediately with `UnboundLocalError` before doing any work,
  on every invocation. Five other commands carried the same redundant import but
  happened to survive it; all six are cleaned up.

### Internal

The package was reorganised into layers (services, a route table for the HTTP
API, a pipeline package for background jobs), the frontend was split out of one
large `index.html` into ES modules with their own stylesheets, and mypy and ruff
gates were added to CI along with a faster, better-organised test suite. A size
ratchet now fails the build when a file or function grows past budget, and the
repository carries the documents a newcomer expects: architecture, contributing,
security, and decision records.

The Node version used to build the desktop app is now pinned and enforced. On
newer versions Electron's install step exited successfully without unpacking the
runtime, so a source checkout produced an app that failed at launch while every
check stayed green; `npm ci` now refuses an unsupported Node, and both `make
setup` and CI verify the binary is actually there.

## [0.1.2] - 2026-07-29

### Changed

- The Pets grids now update in place while detection runs, instead of rebuilding
  the whole section on every change. Scroll position and an in-progress review no
  longer get reset out from under you.

### Fixed

- Console windows no longer flash repeatedly during video processing on Windows.
  The desktop build owns no console of its own, so Windows opened and closed one
  for every spawned `ffmpeg`/`exiftool` call — tens of thousands of flashes over a
  full pipeline run on a large archive.

## [0.1.1] - 2026-07-28

### Added

- Places can now be merged by dragging one onto another, the same way People and
  Pets already could.
- The Library grid can be sorted oldest-first as well as newest-first, and now
  shows which search produced the results on screen, with a way to clear it.

### Changed

- The sidebar pipeline status gives each running stage its own readable progress
  row (label, percentage, and a progress bar) instead of overlapping spinners and
  bare percentages.
- Face clustering now grades every detection for quality and keeps low-quality
  faces (blurry, tiny, badly framed) out of the clustering process entirely,
  instead of only using that signal advisorially. This prevents a handful of weak
  detections from bridging two different people into one cluster.

### Fixed

- Fixed a bug where an archive's catalogue could be filed under the wrong internal
  id: on the affected install this hid over 99% of a 97,000-file archive, kept the
  Overview numbers from ever moving, and made the scan stage rescan the entire
  archive over and over without making progress.
- A fully-scanned archive is no longer rescanned repeatedly; scan completion is
  now recorded explicitly instead of inferred from a count that could never
  reliably reach zero.
- The People grid now updates in place instead of rebuilding itself on every
  detection poll, which used to drop loaded pages, reset scroll position, and
  interrupt an in-progress "Same person?" review.

## [0.1.0] - 2026-07-26

First public release. Trove catalogues a large, messy media collection —
photos, videos, audio, documents, phone dumps and Google Takeout exports —
without ever moving, renaming, or modifying an original file. At this release
it could: scan incrementally and resumably; read dates and GPS from Takeout
sidecars, embedded metadata, filenames and file timestamps, picking a best
date by a documented priority order; find byte-identical and visually similar
duplicate copies and pick a canonical one; build a timeline, library, folder
view and item inspector; cluster geotagged media into named places; detect and
cluster faces into people and detect cats, dogs, birds and horses into pets,
including inside video keyframes; merge, unmerge, and manually tag people and
pets; search the library by description when semantic indexing was enabled;
and correct photo orientation for display without touching the file on disk.

[Unreleased]: https://github.com/gapsa-0/Trove/compare/v0.3.0...HEAD
[0.3.1]: https://github.com/gapsa-0/Trove/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/gapsa-0/Trove/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/gapsa-0/Trove/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gapsa-0/Trove/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/gapsa-0/Trove/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gapsa-0/Trove/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gapsa-0/Trove/releases/tag/v0.1.0
