"""Sentinel device agent.

Standard library only, by design: the installer needs nothing on the target
device beyond python3, so enrollment never depends on apt or PyPI being
reachable. Nothing here stays resident — cron invokes `beat` once a minute and
the process exits.
"""

__version__ = "1.0.0"
