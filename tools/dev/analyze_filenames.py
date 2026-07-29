#!/usr/bin/env python3
"""Diagnostic: measure the filename date parser against a real directory tree.

Reports the match rate, confidence distribution, a sample of the lowest-
confidence matches (to eyeball false positives), and clusters the *unmatched*
names that contain a 6+ digit run (candidate missed date patterns).

Usage:
    python3 tools/analyze_filenames.py /path/to/archive

Use it whenever new material arrives to decide if filename_dates.py needs new
patterns. It only reads names (no file contents), so it is safe to run while a
scan is in progress.
"""

from __future__ import annotations

import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from organize_archive.scan.walker import is_ignored          # noqa: E402
from organize_archive.metadata import filename_dates as fd    # noqa: E402


def skeleton(s: str) -> str:
    s = re.sub(r"\d", "D", s)
    return re.sub(r"[A-Za-z]+", "x", s)


def main(root: str) -> int:
    names = [f for _, _, fn in os.walk(root) for f in fn if not is_ignored(f)]
    total = len(names)
    if not total:
        print("No files found.")
        return 1

    matched, unmatched = 0, []
    conf = collections.Counter()
    low = []
    for n in names:
        r = fd.parse(n)
        if r:
            matched += 1
            conf[round(r[1], 2)] += 1
            if r[1] <= 0.4 and len(low) < 25:
                low.append((r[0].isoformat(), n))
        else:
            unmatched.append(n)

    print(f"TOTAL: {total}")
    print(f"MATCHED:   {matched} ({100*matched/total:.1f}%)")
    print(f"UNMATCHED: {len(unmatched)} ({100*len(unmatched)/total:.1f}%)")
    print("\nConfidence distribution:")
    for c in sorted(conf, reverse=True):
        print(f"  {c}: {conf[c]}")

    print("\nLowest-confidence matches (check for false positives):")
    for iso, n in low:
        print(f"  {iso}  <-  {n}")

    date_like = [n for n in unmatched if re.search(r"\d{6,}", n)]
    print(f"\nUnmatched with a 6+ digit run ({len(date_like)}):")
    sk, ex = collections.Counter(), {}
    for n in date_like:
        k = skeleton(n)
        sk[k] += 1
        ex.setdefault(k, n)
    for k, c in sk.most_common(35):
        print(f"  {c:>6}  {k:<32}  e.g. {ex[k]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
