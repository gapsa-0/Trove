# 0023. An action is either asked for or offered, and each has one control

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

[0002](0002-no-frontend-framework.md) rules out a component library, and that is
the right call for a local single-user app with no build step. It leaves a gap
it does not mention: there is nowhere a control *lives*. `ARCHITECTURE.md`'s
"where does X live" table maps a screen to a stylesheet, so a contributor adding
a button opens the stylesheet named after their screen — where there is nothing
to copy from, and nothing to say a shared control already exists.

Two families had drifted as a result, and neither drift was ever decided.

**Four places offer a way back; three drew it differently.** The sidebar and a
person's page drew a stroked chevron (`.back-control`), the sidebar's in accent
text beside the words "All archives". A pet's page drew a typed `←` — and
because `.facetopbar .back-control span { display: none }` hides the label on a
group's page, that shipped as a bare text glyph in a 38px square where People
drew an 18px chevron, with no `aria-label` where People had one. The search
results drew a third: `.back-btn`, a second copy of the whole control with its
own padding, radius and hover, and a typed arrow again.

Being text also cost the sidebar's the collapsed rail. `nav.collapsed .back` hid
it along with every other label, which left a collapsed sidebar with no way back
to the archives at all — the one control on that screen there is no second route
to.

**Five controls were the same "offered, not insisted on" button**, with three
corner radii, three colours, three weights and three hovers between them:

| Control | Radius | Colour | Weight |
| --- | --- | --- | --- |
| `.doc-info` — *How this works* | 999px | `--muted` | inherited |
| `.manage-features` | 8px | `--text` | 620 |
| `.selbtn` — the selection bar | 999px | `--text` | 600 |
| `.dupkeep` — *Keep* under a copy | 7px | `--muted` | 600 |
| `.selectstart` — *Select* | (a bare `.linkbtn`) | `--accent` | — |

Each was locally reasonable, written next to the screen that needed it. The cost
was not untidiness. `.manage-features` was the only one in `--text` at 620 —
which is `.btn`'s weight — so at the foot of the Library health panel it read as
that panel's main action, directly under the panel's actual main action
(*Resume all*, `.btn.sec`). Two tiers had collapsed into one ambiguous middle.

**And eighteen actions were accent words with no box** (`.linkbtn`): *Set* and
*Copy* beside a heading in the inspector, *Clear filters*, *Clear search*, *Put
back*, *Undo*, *Remove name*. In a interface where the only other blue words are
links into the documentation, a control that looks like a link is a control that
reads as leaving the app. It also gave the same act two appearances depending on
where it sat — *Keep* under a duplicate copy is an outlined button, *Put back*
under a group set aside was a blue word.

Two fixes were tempting. A component library or Storybook is what a larger
frontend would reach for, and [0002](0002-no-frontend-framework.md) already
rejected the toolchain it assumes. A bespoke stylelint rule flagging a
button-shaped rule outside `theme.css` was the other, and is rejected here:
`check_handlers.py` and `check_sizes.py` earn their keep because they answer
questions with a yes or a no, and "is this a new *kind* of control" is a
judgement. A checker guessing at it would cost more in false positives than the
drift it caught.

## Decision

**Two tiers, by what the action is to the person reading the screen.**

1. **Asked for** — `.btn`, or `.btn.sec` where a filled accent would shout: the
   thing the screen or panel exists to do. *Search*, *Save changes*, *Resume
   all*, *Create & attach*.
2. **Offered** — `.quietbtn`: available, not urged. Outlined, muted, taking the
   accent only when pointed at. *How this works*, *Manage features*, *Select*,
   *Keep*, *Set*, *Copy*, *Put back*, *Undo*, and each action on the selection
   bar.

`.quietbtn` takes the shape `.doc-info` already wore, because that control is on
every screen in the app and is therefore the one a reader has already learned.

**Where it sits beside a heading or inside a row, it is the same control in less
room** — `.quietbtn.sm`, a padding and a font size. Not a third tier: *Copy*
next to *Detected text* and *Manage features* at the foot of a panel are the
same offer, made where there is more or less space for it.

**There is no link-shaped button.** A blue word with no box reads as a link, and
in this app the only blue words are links into the documentation. An action that
stays on the screen wears a box.

**There is one back control**, `backControl()` in `static/js/router.js`: a
chevron in a quiet square that takes a background when pointed at, sized 30px
everywhere but a group's own page, whose 82px bar takes 38px.

**It draws no label.** The treatment is a group's page's, which had it right —
what a back control needs to say is "back", and the chevron says it in a mark
everyone reads. Where it goes is the `title` and the accessible name rather than
words on screen, because the screen already answers it: the thing you are
looking at is the thing you would be leaving. It is muted rather than accent for
the same reason — this is furniture at the edge of a screen, not an offer being
made, and as accent text it was the loudest thing in the sidebar, above the
archive it belongs to.

Its handler is attached rather than written into the markup — a helper
interpolating `onclick="…"` would hide those functions from
`check_handlers.py`, which reads inline handlers as text, and would keep on
`window` three functions nothing outside their own screen calls. The nav's own
is static markup in `index.html` and matches by hand.

**A new control is a modifier on an existing one until it can say what makes it
a different kind of thing.** `.dupkeep` is `.quietbtn` plus a width and a state
colour; `.selbtn.is-danger` is `.quietbtn` plus a hover colour. Neither is a new
control, and neither re-declares a border, a radius or a font.

Not covered, and deliberately: `.metric-switch` and `.map-viewtoggle` are
segmented switches, which choose between views rather than perform an act, and
`.chip` is a removable filter token. Those are different kinds of thing and keep
their own shapes.

## Consequences

- `.quietbtn` lives in `theme.css`, the one stylesheet not named after a screen,
  which is where somebody asking "what do I reach for" can find it. This ADR is
  the other half of that answer: the file says what the control looks like, and
  this says which of the three to pick.
- The hierarchy is legible again where it had collapsed. *Resume all* over
  *Manage features* in one panel now reads as a strong action and a quiet one,
  rather than as two buttons of unrelated design.
- **`.linkbtn` is retired**, and its two declarations went with it. The
  inspector re-grounds the shared control on glass instead
  (`.viewer .info .quietbtn`), which is what that stylesheet exists to do: the
  theme's `--line` and `--muted` are chosen against a page background and are
  invisible over a photograph.
- **Two hidden-group cards were built differently from every other card**, and
  putting a box where a blue word had been is what exposed it: neither wrapped
  its name and count in `.pmeta-text`, so all three children became items in the
  card's flex row and the count wrapped. They now have the shape a card in the
  grid has, with *Put back* where the ⋯ sits.
- **Class names survive as hooks.** Markup is `class="quietbtn doc-info"`, so
  `.doc-info` still addresses that control in the browser tier and still has
  somewhere for a per-instance rule to live if it earns one.
- Nothing enforces this mechanically, and the "Context" section says why. The
  failure mode is a new class re-declaring the same eight properties, which is
  visible in a diff to anyone who knows to look — which is what this record is
  for.
- **The collapsed sidebar keeps its way out.** A 30px icon fits a 68px rail
  where a row of text did not, so the rule that hid it there is gone — a fix
  that falls out of the decision rather than one made alongside it.
- **`.facetopbar .back-control` is now only a size**, not a restyle: the one
  override left is the larger square a taller bar needs. What it used to do —
  hide the label, recolour, add a hover — the shared control now does for
  everyone.
- **The label is gone from the screen, and that is the part most likely to be
  questioned.** It is a real trade: an unlabelled chevron is less discoverable
  than "All archives", and in the sidebar it is the only exit from an archive.
  Kept because the alternative reintroduces two treatments — a labelled one and
  an icon one — which is the thing this record exists to stop, and because the
  destination survives in the tooltip and the accessible name.
- No behaviour change beyond the pet page's back button, which gained the
  chevron the rest of the app draws and the `aria-label` it never had.
