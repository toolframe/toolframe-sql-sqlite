"""Reject publication unless the selected Git ref matches project.version."""

import sys
import tomllib
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/check_release.py refs/tags/vVERSION")
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text())["project"]
    expected = f"refs/tags/v{project['version']}"
    if sys.argv[1] != expected:
        raise SystemExit(f"Publishing requires {expected}; received {sys.argv[1]!r}")
    print(f"Release tag matches {project['name']} {project['version']}")


if __name__ == "__main__":
    main()
