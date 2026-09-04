"""trove — local, read-only cataloging of a family multimedia archive.

See ARCHITECTURE.md for the design and hard rules. The most important one:
this package never writes to, moves, renames, or deletes anything under a
source root. All output goes to the database and cache directory.
"""

__version__ = "0.3.1"
