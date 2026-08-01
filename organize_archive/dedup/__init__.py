"""Duplicate detection: exact-content and visually-identical grouping, with
deterministic canonical-copy selection (see ``exact.py``).

Groups are flagged, not deleted -- non-canonical copies stay on disk and in
the database, just hidden from default browsing.
"""
