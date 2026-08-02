# The quarterly repository review

About an hour, once a quarter. `.github/workflows/repo-review.yml` opens an
issue on the first of January, April, July and October so this does not depend
on anyone remembering.

The automated checks in CI hold the line at every commit. This is the part they
cannot do: noticing a slow trend, and reading the reasons behind the rules to
see whether they are still the reasons.

## 1. What grew?

```
.venv/bin/python tools/dev/check_sizes.py --list
git ls-files | xargs wc -l | sort -rn | head -20
```

The first prints everything already over budget. The second is the one that
matters here — it shows what is *approaching* 600 lines and is not on any list
yet. Something at 550 is a splitting job now, while it is still one concern
growing; at 900 it is three concerns tangled together and a much worse afternoon.

Then read the allowlist in `tools/dev/check_sizes.py`. Did it gain an entry this
quarter? Find the commit (`git log -p tools/dev/check_sizes.py`) and check that
the body gives the reason it was supposed to give. An entry that can be deleted
because the file shrank is deleted by CI's stale-entry check already, so
anything still there is a live exception.

## 2. Is the suite still fast?

```
make test-fast                                   # should stay ~2s
.venv/bin/python -m pytest -q --durations=25
```

`make test-fast` is the per-save loop and stops being used the moment it stops
feeling instant. If the full suite has crept past a minute, the `--durations`
list says where; the usual cause is a new test paying to build a real archive
when `tests/factories.py` would have done.

## 3. Did coverage drop where it matters?

```
.venv/bin/python -m pytest -q --cov=organize_archive --cov-report=term-missing
```

Not a number to defend — coverage is mapped here, not gated. The question is
narrower: did a module that changed a lot this quarter lose coverage? That is
the signature of code added without tests, which is the thing the number is
actually a proxy for.

## 4. Dead code

```
.venv/bin/python -m ruff check --select F401,F841 .
cd desktop && npm run lint          # no-unused-vars, including the browser modules
```

Then the part no linter does: look for whole modules nothing imports, and for
exported functions nothing calls. **Delete rather than comment out** — git
remembers, and a commented-out block is a question every future reader has to
re-answer.

One caution learnt the hard way: an unreferenced function is not automatically
scaffolding. Several in `web/static/js` read as features someone stopped calling,
and deleting one of those deletes a bug report. Decide per function whether the
caller is missing or the function is.

## 5. Dependencies

Merge the open Dependabot pull requests, or close each with a comment saying
why not. A queue of ignored dependency PRs is worse than no Dependabot: it
trains everyone to ignore the next one, including the security fix.

Pins live in `constraints.txt`; `docs/dev/dependencies.md` explains the extras
and the known-sharp edges.

## 6. Have any decisions been reversed in practice?

Read `docs/adr/`. For each record, ask whether the code still does what it says.
A decision that has been quietly reversed is the most expensive kind of stale
documentation, because the next person reads it, believes it, and builds on it.
If one has been reversed, write the superseding ADR — the point is the trail,
not being right the first time.

Same question for `CLAUDE.md` and `ARCHITECTURE.md`: does every path in them
still exist? An assistant or a newcomer working from a stale map will faithfully
recreate the structure the map describes.
