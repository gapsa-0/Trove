# Troubleshooting

## Where are the logs?

```
trove logs              # the last 200 lines
trove logs --tail 50    # the last 50
trove logs --path       # just the path, e.g. to attach the file to a bug report
```

The file itself:

| OS | Path |
|---|---|
| Linux | `~/.local/share/trove/logs/trove.log` |
| Windows | `%LOCALAPPDATA%\trove\logs\trove.log` |
| macOS | `~/Library/Application Support/trove/logs/trove.log` |

It rotates at 5 MB and keeps three older files (`trove.log.1` … `.3`), so it is
capped at about 20 MB and never needs clearing by hand. `trove logs` reads the
current file only.

For more detail, set `TROVE_LOG_LEVEL=DEBUG` before starting the app. That adds the
scheduler's reasoning — one line per tick saying which stages it saw, what state
each was in, and what it decided to start — which is what answers "why has the
next stage not begun?".

**Reading a job's history.** Every background job logs a `job start` line and
exactly one `job done`, `job cancelled` or `job failed` line. That gives three
distinguishable cases:

- *no `job start` at all* — the scheduler never ran it. Check for `tick:` lines
  at DEBUG, and whether the stage is paused.
- *`job start` with no matching end* — the job is still going, or the app was
  killed while it ran. The stage named in the line is where it was.
- *`job failed`* — the traceback follows it in the log.

Nothing is ever sent anywhere. The log stays on this machine and is only useful
if you choose to attach it to a report; see `privacy-and-data.md`.

## Other problems

If the backend cannot start, check the log above. For a locked
database, close other Archive instances; for an unreachable folder, reconnect/mount
it and reopen.

The database and cache are valuable derived data. Back them up by copying the
app-data folder while Archive is closed. To restore, close Archive and replace its
app-data folder with the backup. Do not alter source media while troubleshooting.

**Pets and People say "unavailable" (development only).** These features need OpenCV
and onnxruntime, which live in the project `.venv`. Running `npm run dev` from a shell
without the venv active launches the backend on the system Python, which lacks them.
Activate the venv (`. ../.venv/bin/activate`) or set `PYTHON=../.venv/bin/python` before
`npm run dev`. Packaged builds bundle their own interpreter and are not affected.
