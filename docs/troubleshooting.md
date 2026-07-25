# Troubleshooting

If the backend cannot start, check the logs in the app-data folder's `logs/`
subdirectory (on Linux, `~/.local/share/organize_archive/logs`). For a locked
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
