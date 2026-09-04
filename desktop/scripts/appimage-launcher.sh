#!/bin/bash
# The AppImage's launcher, and the only place its sandbox question can be
# answered in time.
#
# Chromium sandboxes the renderer with an unprivileged user namespace or, failing
# that, a setuid helper; Ubuntu 23.10 and later grant the first only to programs
# an AppArmor profile covers. An AppImage installs nothing, so it gets no
# profile, and it is mounted nosuid, so its helper cannot be setuid either --
# leaving Chromium with neither, which it answers by aborting before the window.
#
# It has to be settled here because Chromium reads --no-sandbox off the command
# line before it runs a line of the app's own JavaScript: nothing inside Trove
# gets a say (desktop/src/sandbox.cjs, which explains the same thing from the
# other side, and reports afterwards what this decided).
#
# So: keep the sandbox everywhere it can work -- every distribution that does not
# restrict user namespaces, which is most of them -- and where it provably cannot,
# start without it rather than not at all. Trove's renderer loads nothing but this
# machine's own loopback service, so what is lost is a layer of defence and not
# the only one. Set TROVE_KEEP_SANDBOX=1 to refuse the fallback, which is what
# someone who has written an AppArmor profile for trove-desktop-bin wants.
set -e

here="$(dirname "$(readlink -f "$0")")"
binary="$here/trove-desktop-bin"

restricted="$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)"
# `unconfined`, or a profile name -- possibly followed by its mode in
# parentheses, as in `trove-desktop (unconfined)`, hence the trim.
confinement="$(cat /proc/self/attr/apparmor/current 2>/dev/null \
  || cat /proc/self/attr/current 2>/dev/null || echo unconfined)"

if [ "$restricted" = "1" ] && [ "${confinement%% *}" = "unconfined" ] && [ -z "$TROVE_KEEP_SANDBOX" ]; then
  echo "Trove: starting without the renderer sandbox -- this system restricts" >&2
  echo "unprivileged user namespaces and an AppImage cannot install the AppArmor" >&2
  echo "profile that would grant them. The .deb can: see docs/install-linux.md." >&2
  exec "$binary" --no-sandbox "$@"
fi

exec "$binary" "$@"
