"""Application loader.

The implementation is split into numbered modules to keep repository writes
reviewable. Each part executes in this module's namespace, producing the Flask
``app`` object consumed by Gunicorn.
"""
from pathlib import Path

_BASE = Path(__file__).resolve().parent
for _part in (
    "_bridge_part1.py",
    "_bridge_part2.py",
    "_bridge_part3.py",
    "_bridge_part4.py",
    "_bridge_oauth_fix.py",
    "_bridge_oauth_diagnostics.py",
):
    _path = _BASE / _part
    exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), globals(), globals())
