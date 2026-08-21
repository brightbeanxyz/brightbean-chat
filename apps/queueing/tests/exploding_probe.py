"""A module that raises on import, for the housekeeping resolution test.

Stands in for a sibling app that is installed but misconfigured — the case that
must not be allowed to abort the hourly sweep.
"""

raise RuntimeError("this module is broken on import, on purpose")
