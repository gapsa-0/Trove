## What does this change?

<!-- Short description of the change and why it's needed. -->

## Checklist

The definition of done, from `CONTRIBUTING.md`. Say so explicitly if an item
genuinely does not apply, rather than leaving it blank and ambiguous.

- [ ] `make check` passes — lint, format, types, handler and size checks, full suite
- [ ] A bug fix includes a regression test that fails without the fix
- [ ] A new feature has tests for its normal path and at least one edge case
- [ ] No new file over 600 lines and no new function over 80 (`make sizes`); a new
      allowlist entry is justified in the commit body
- [ ] User-visible changes are reflected in `README.md` / `docs/`, and have a
      `CHANGELOG.md` entry under `[Unreleased]`
- [ ] New derived data carries its provenance (source + confidence, where applicable)
- [ ] A long operation is still resumable and idempotent — killed mid-run and re-run,
      it picks up rather than redoing or double-counting
- [ ] Nothing writes to a source archive root
- [ ] A decision that closes off an alternative has an ADR under `docs/adr/`
- [ ] Commit messages have no AI-attribution trailer (no `Co-Authored-By`, no
      "generated with" footer)
