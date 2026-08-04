"""Resumable directory walking and change detection (``walker.py``): finds
files under configured roots, applies ignore rules, and upserts catalog rows
incrementally without re-reading unchanged files.
"""
