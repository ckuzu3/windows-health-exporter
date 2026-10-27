"""Entry point for `python -m windows_health_exporter`.

Covered by a subprocess test rather than by an import, which is why coverage cannot see
it: the point of the test is that starting the module as a program works.
"""

from windows_health_exporter.cli import main

if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess
    raise SystemExit(main())
