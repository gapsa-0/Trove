"""The Library health panel: the pipeline as the Overview draws it.

One row per stage, and each row is a mark, a name, one line of state and the
button that stops just that stage. All of it is DOM and CSS with no other
coverage -- the API tests prove the snapshot is right, and every failure this
file has caught was a correct snapshot rendered into a row that said nothing,
could not be paused, or collapsed into the rail's own grid track.

The archive fixture keeps the pipeline paused, since a browser test must not
start real stages, so anything about a *finished* pipeline stands in for the
server with a snapshot of its own (see _ALL_DONE_JS).
"""

from __future__ import annotations


def test_library_health_draws_the_pipeline_as_a_chain(open_app):
    """The Overview's rail. It is DOM and CSS with no other coverage, and the
    thing it says — Indexing and Duplicates first, everything else hanging off
    them — is the same thing the setup screen says before any of it runs.

    Asserted through the trunk marking rather than by counting rows, because
    which optional stages an archive runs is its own business; that the first
    two are the ones nobody can decline is not.
    """
    with open_app("overview") as app:
        # A stage's node, not any node: the "Checking for work…" placeholder is a
        # row too, and has no rail, no head and no label to read -- and the row
        # that opens the Features sheet hangs its own node off the end of this
        # same rail, from markup that is on screen before the first poll has
        # said what the chain is.
        app.wait_for(".health-task .health-node")
        labels = app.tab.evaluate(
            "[...document.querySelectorAll('.health-task')]"
            ".map(e => [e.classList.contains('trunk'),"
            " e.querySelector('.health-task-head').textContent.trim()])"
        )
        assert [label for trunk, label in labels if trunk] == ["Indexing", "Duplicates"]
        assert labels[0][1] == "Indexing" and labels[1][1] == "Duplicates"
        # One node per row, and a rail to hang them on.
        assert app.count(".health-task .health-node") == len(labels)
        assert app.errors() == []


def test_a_health_row_without_a_rail_still_gets_the_full_width(open_app):
    """The rail is two grid tracks, and a row that has no rail — the
    "Checking for work…" placeholder, since there is no chain to draw until
    the first poll says what it is — flowed into the 20px track and wrapped
    one letter per line. Asserted on the layout rule rather than on the
    placeholder's markup, because the rule is what has to hold for any row.
    """
    with open_app("overview") as app:
        # A stage's node: the chain's closing node is drawn before the poll that
        # fills the grid this reaches into.
        app.wait_for(".health-task .health-node")
        width = app.tab.evaluate("""
          (() => {
            const grid = document.querySelector('.health-grid');
            grid.insertAdjacentHTML('beforeend',
              '<div class="health-task" id="railless">'
              + '<div class="health-task-body">x</div></div>');
            const w = document.querySelector('#railless .health-task-body').offsetWidth;
            document.getElementById('railless').remove();
            return w;
          })()
        """)
        assert width > 200, f"a railless row collapsed to {width}px"
        assert app.errors() == []


# A pipeline snapshot with every stage finished, served in place of the real
# one. The browser fixture keeps the pipeline paused -- stages must not start
# behind a test -- and a paused card reports "Paused" whatever it has done, so
# the finished wording is unreachable without standing in for the server. Stubbing
# `fetch` for this one URL is how the pause test above reaches its own window too.
_ALL_DONE_JS = """(() => {
  const snap = {
    // The client checks a snapshot is about the archive that is open before it
    // takes it, so standing in for the server means answering that too. Read
    // off the hash rather than hardcoded: the fixture's root id is its own.
    root_id: +location.hash.match(/archive\\/(\\d+)/)[1],
    paused: false, overall: 'idle', extra: [],
    stages: [...document.querySelectorAll('.health-task')].map((e, i) => ({
      id: ['scan', 'dedup', 'places', 'detect', 'semantic', 'text'][i],
      label: e.querySelector('.health-task-head').textContent.trim(),
      icon: '', state: 'up_to_date', message: null, progress: null,
      pending: 0, counted: true, always_runs: i < 2, next: false,
      pausing: false, paused: false, stalled: false, waiting_on: null,
    })),
  };
  window.__realFetch = window.fetch;
  window.fetch = (...args) => String(args[0]).includes('/api/pipeline?')
    ? Promise.resolve(new Response(JSON.stringify(snap),
      { headers: { 'Content-Type': 'application/json' } }))
    : window.__realFetch(...args);
})()"""


def test_every_finished_health_row_says_what_its_stage_found(open_app):
    """A stage's row is a mark, a name and one line of state. The finished line
    is a per-card sentence quoting the numbers the Overview already holds -- so
    a card added to the pipeline without one shows a dot and nothing at all,
    which is what the text card did for as long as it has existed.

    Asserted across every row rather than for that card by name, so the next
    card added cannot arrive silent in the same way.
    """
    with open_app("overview") as app:
        app.wait_for(".health-task .health-node")
        app.tab.evaluate(_ALL_DONE_JS)
        app.tab.evaluate("import('/static/js/overview.js').then(m => m.startPoll())")
        app.tab.wait_for(
            "!document.querySelector('.health-task-state .spin')"
            " && ![...document.querySelectorAll('.health-task-state')]"
            "   .some(e => e.textContent.includes('Paused'))",
            what="the finished snapshot to land",
        )
        rows = app.tab.evaluate(
            "[...document.querySelectorAll('.health-task')].map(e => ["
            " e.querySelector('.health-task-head').textContent.trim(),"
            " e.querySelector('.health-task-state').textContent.trim()])"
        )
        app.tab.evaluate("window.fetch = window.__realFetch")

        silent = [name for name, state in rows if not state]
        assert not silent, f"finished health rows with nothing to say: {silent}"
        # The one this was written for: both text features fill one index, so
        # what it reports is how much of the archive it has read.
        assert any("read" in state for name, state in rows if "text" in name)
        assert app.errors() == []


def test_every_stage_can_be_stopped_on_its_own(open_app):
    """Each card carries its own pause button, on top of the whole-pipeline
    switch: the point of it is stopping one stage and letting the rest run.

    The button used to be gated on a hardcoded list of card ids kept in the
    frontend, which nobody updated when the text stage was added -- so the
    slowest stage in the pipeline, the one most worth stopping on its own, was
    the one card that could not be. Asserted across every row for that reason.
    """
    with open_app("overview") as app:
        app.wait_for(".health-task .health-node")
        rows = app.tab.evaluate(
            "[...document.querySelectorAll('.health-task')].map(e => ["
            " e.querySelector('.health-task-head').textContent.trim(),"
            " !!e.querySelector('.health-task-btn')])"
        )
        assert rows, "no health rows to check"
        mute = [name for name, has in rows if not has]
        assert not mute, f"health rows with no way to pause them: {mute}"
        assert app.errors() == []


def test_the_pause_button_does_not_guess_while_it_is_still_checking(open_app):
    """The window between opening an archive and its first pipeline snapshot.

    A pause is per-archive and persisted, so an archive that was paused when it
    was last closed opens paused -- but the client starts with no snapshot, and
    reading that absence as "not paused" made the button announce "Pause all"
    over a pipeline that was already stopped. Pressing it then posted
    ``paused: true`` to pause it again, which is the half that made this a bug
    rather than a wrong word: the one control that could have got the archive
    running again instead confirmed it stopped.

    Driven by holding the snapshot back, because that is the real condition --
    the button is wrong for exactly as long as the answer has not arrived.
    """
    with open_app("overview") as app:
        app.wait_for("#pause-btn")
        # Let the app's own first poll finish before touching fetch, because
        # stubbing cannot recall a request that is already in flight -- and this
        # test is *about* the state before any snapshot, so one arriving late is
        # not a detail of the setup but the thing being suppressed. It lands in
        # `refreshPipeline`, which assigns `S.pipeline` with no generation guard,
        # so the null set below is overwritten and the button re-enabled. On a
        # developer machine that reply comes back before the stub is even
        # installed; on a loaded runner it arrived between the wait and the
        # assertion, which is what made this the browser tier's flakiest test.
        #
        # Stop the poller so no *new* request is issued, then wait for the one
        # outstanding to have landed -- the placeholder gives way to real cards
        # exactly when it does.
        app.tab.evaluate(
            "import('/static/js/overview.js')"
            ".then(m => { m.stopPoll(); window.__pollStopped = true; })"
        )
        app.tab.wait_for("window.__pollStopped === true", what="the poller to stop")
        app.tab.wait_for(
            "!(document.getElementById('syncstatus') || {}).textContent"
            ".includes('Checking for work')",
            what="the first pipeline snapshot to land",
        )
        app.tab.evaluate("""
          (() => {
            window.__realFetch = window.fetch;
            window.__pausePosts = 0;
            window.fetch = (...args) => {
              const url = String(args[0]);
              if (url.includes('/api/pipeline/pause')) window.__pausePosts++;
              // Never answers: the snapshot is what has not arrived yet.
              if (url.includes('/api/pipeline?')) return new Promise(() => {});
              return window.__realFetch(...args);
            };
          })()
        """)
        app.tab.evaluate(
            "import('/static/js/state.js').then(m => { m.S.pipeline = null;"
            " showSection('overview', true); })"
        )
        app.tab.wait_for(
            "(document.getElementById('pause-btn') || {}).textContent === 'Checking…'",
            what="the pause button to say it does not know yet",
        )

        # Both halves in one evaluate: "says it does not know" and "cannot be
        # pressed" are one claim, and `renderPauseControl` sets them in one
        # synchronous pass. Read over two round-trips they could only ever
        # disagree because something repainted in between -- which is a fact
        # about the test, not about the button.
        assert (
            app.tab.evaluate("""
          (() => { const b = document.getElementById('pause-btn');
                   return b.textContent === 'Checking…' && b.disabled === true; })()
        """)
            is True
        )
        # ...and stays inert if something reaches it anyway.
        app.tab.evaluate("togglePipelinePause()")
        assert app.tab.evaluate("window.__pausePosts") == 0, "it posted a pause it could not know"

        app.tab.evaluate("window.fetch = window.__realFetch")
        assert app.errors() == []


def test_unanswered_status_polls_do_not_stack_up(open_app):
    """A poll tick that outlasts its interval must be waited for, not piled on.

    The first `/api/pipeline` after an archive is opened waits for its tree to
    be counted -- about 20s for 97k files on a cold cache -- and the two pollers
    that ask for it run on 1.2s and 2s intervals. Each interval that passed
    added another request. The duplicated work is not the damage (the server
    collapses those onto one walk); the damage is that a browser allows about
    six connections per origin, so a handful of stacked polls is the whole
    budget and every other request the page makes -- thumbnails, library pages,
    search -- queues behind them. That is what turned a slow health panel into
    an archive where nothing at all worked.

    Asserted as "the count stops growing" rather than on an exact number,
    because there are two independent pollers and the point is that neither
    accumulates -- not which of them got a request away first.

    Driven by a snapshot that never answers, because that is the real condition:
    the polls stacked for exactly as long as the first one was outstanding.
    """
    with open_app("overview") as app:
        app.wait_for("#pause-btn")
        app.tab.evaluate("""
          (() => {
            window.__realFetch = window.fetch;
            window.__pipeCalls = 0;
            window.fetch = (...args) => {
              const url = String(args[0]);
              // Never answers, so every later tick meets a poll still in flight.
              if (url.includes('/api/pipeline?')) {
                window.__pipeCalls++;
                return new Promise(() => {});
              }
              return window.__realFetch(...args);
            };
          })()
        """)
        # Restart the Overview poller so its next tick is a stubbed request; the
        # sidebar chip's poller has been running since the archive was opened.
        app.tab.evaluate("import('/static/js/overview.js').then(m => m.startPoll())")
        app.tab.wait_for("window.__pipeCalls >= 1", what="a poller to issue its first request")
        # Let both pollers get one request away and reach their guard, then take
        # the baseline. Timed in the page: `wait_for` re-evaluates its expression
        # on every poll, so a fresh promise per call would never be the same
        # promise twice -- a flag a timer sets once is.
        app.tab.evaluate("setTimeout(() => { window.__settled = true; }, 2500)")
        app.tab.wait_for("window.__settled === true", timeout=20.0, what="both pollers to tick")
        before = app.tab.evaluate("window.__pipeCalls")
        # ...and now several more intervals of both, with nothing answered.
        app.tab.evaluate("setTimeout(() => { window.__waited = true; }, 6000)")
        app.tab.wait_for("window.__waited === true", timeout=20.0, what="five more poll intervals")
        after = app.tab.evaluate("window.__pipeCalls")

        app.tab.evaluate("import('/static/js/overview.js').then(m => m.stopPoll())")
        app.tab.evaluate("window.fetch = window.__realFetch")
        assert after == before, (
            f"{after - before} more polls stacked on snapshots that never answered"
        )
        # One in flight per poller, and there are two of them.
        assert after <= 2, f"{after} polls outstanding at once"
        assert app.errors() == []


def test_a_snapshot_for_another_archive_is_not_taken(open_app):
    """A poll started before the user switched archives lands afterwards, and
    the client used to take it whatever it was about -- so the archive just
    opened reported the stages, counts and pause state of the one left behind.

    Rare while the snapshot was fast. What makes it worth guarding is that the
    first snapshot of an archive waits for its tree to be counted, ~20s on a
    large one, and switching away from an archive that is thinking about it is
    exactly what someone does. The window is the whole of that wait.

    Driven by a snapshot that names a different root rather than by really
    switching archives, because the payload's `root_id` is what the guard reads
    and a second seeded archive would prove nothing more.
    """
    with open_app("overview") as app:
        app.wait_for(".health-task .health-node")
        # Let the real first snapshot land, then stop the poller so nothing
        # overwrites what this test is about to inspect.
        app.tab.wait_for(
            "!(document.getElementById('syncstatus') || {}).textContent"
            ".includes('Checking for work')",
            what="the first pipeline snapshot to land",
        )
        app.tab.evaluate("import('/static/js/overview.js').then(m => m.stopPoll())")
        # Stashed on window rather than returned from the import: `evaluate`
        # does not await promises, so reading through one yields {}.
        app.tab.evaluate("import('/static/js/state.js').then(m => { window.__S = m.S; })")
        app.tab.wait_for("!!window.__S", what="the state module")
        mine = app.tab.evaluate("window.__S.pipeline.root_id")
        assert isinstance(mine, int), f"no root_id on the snapshot the client took: {mine!r}"

        # A snapshot about some other archive: paused, and with no stages at all,
        # so taking it would be plain on the panel as well as in the state.
        app.tab.evaluate(f"""
          (() => {{
            window.__realFetch = window.fetch;
            const alien = {{ root_id: {mine + 999}, overall: 'paused', paused: true,
                            stages: [], extra: [], paused_stages: [] }};
            window.fetch = (...args) => String(args[0]).includes('/api/pipeline?')
              ? Promise.resolve(new Response(JSON.stringify(alien),
                {{ headers: {{ 'Content-Type': 'application/json' }} }}))
              : window.__realFetch(...args);
          }})()
        """)
        app.tab.evaluate("import('/static/js/overview.js').then(m => m.startPoll())")
        app.tab.evaluate("setTimeout(() => { window.__waited = true; }, 3000)")
        app.tab.wait_for("window.__waited === true", timeout=20.0, what="several polls to land")

        kept = app.tab.evaluate("window.__S.pipeline.root_id")
        app.tab.evaluate("import('/static/js/overview.js').then(m => m.stopPoll())")
        app.tab.evaluate("window.fetch = window.__realFetch")

        assert kept == mine, f"took a snapshot about root {kept} while root {mine} was open"
        # ...and the panel still draws this archive's chain rather than emptying.
        assert app.count(".health-task .health-node") > 0, "the alien snapshot emptied the panel"
        assert app.errors() == []
