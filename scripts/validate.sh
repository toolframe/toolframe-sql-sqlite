#!/usr/bin/env bash
# Run the same validation locally and in GitHub Actions without touching siblings.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
sdk_source=${TOOLFRAME_SDK_SOURCE:-"$repo_root/../toolframe-sdk"}
sdk_revision=ef91b609137a5fe7bae8e0c893e74b096ff06ffe
python_version=3.14.7
output=${1:-"$repo_root/dist/ci"}

if [[ $# -gt 1 ]]; then
  echo 'Usage: bash scripts/validate.sh [empty-output-directory]' >&2
  exit 1
fi
mkdir -p "$output"
output=$(cd -- "$output" && pwd)
if [[ -n "$(ls -A "$output")" ]]; then
  echo "Output directory must be empty: $output" >&2
  exit 1
fi
git -C "$sdk_source" cat-file -e "$sdk_revision^{commit}"

work_root=$(mktemp -d "${TMPDIR:-/tmp}/toolframe-storage-sqlite-ci.XXXXXX")
trap 'rm -rf "$work_root"' EXIT
echo "Isolated workspace: $work_root"
echo "SDK revision: $sdk_revision"
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME
export UV_CACHE_DIR="$work_root/uv-cache"
export UV_PROJECT_ENVIRONMENT="$work_root/test-venv"
export UV_PYTHON="$python_version"
export UV_LINK_MODE=copy

project="$work_root/toolframe-storage-sqlite"
mkdir -p "$project" "$work_root/toolframe-sdk" "$work_root/wheelhouse" "$work_root/dist"
# Include current tracked and untracked, non-ignored files, never an existing venv/dist.
git -C "$repo_root" ls-files --cached --others --exclude-standard -z \
  | LC_ALL=C tar -C "$repo_root" --null -T - -cf - \
  | LC_ALL=C tar -C "$project" -xf -
git -C "$sdk_source" archive "$sdk_revision" | LC_ALL=C tar -C "$work_root/toolframe-sdk" -xf -
cd "$project"

uv sync --locked --group dev --python "$python_version"
"$UV_PROJECT_ENVIRONMENT/bin/python" --version
"$UV_PROJECT_ENVIRONMENT/bin/ruff" check src tests scripts
"$UV_PROJECT_ENVIRONMENT/bin/python" -m pytest -q
bash -n scripts/validate.sh

# Build with publishable metadata; the wheel is built from the new sdist by default.
uv build --no-sources --python "$python_version" --out-dir "$work_root/wheelhouse" "$work_root/toolframe-sdk"
uv build --no-sources --python "$python_version" --out-dir "$work_root/dist" "$project"
uv venv --python "$python_version" "$work_root/package-tools"
uv pip install --python "$work_root/package-tools/bin/python" 'twine==7.0.0'
"$work_root/package-tools/bin/python" -m twine check --strict "$work_root/dist/"*

"$work_root/package-tools/bin/python" - "$work_root/dist" "$project" <<'PY'
import email
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

dist, project = map(Path, sys.argv[1:])
metadata = tomllib.loads((project / "pyproject.toml").read_text())["project"]
wheels, sdists = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
assert len(wheels) == len(sdists) == 1, "Expected one wheel and one sdist"
with zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sdists[0]) as sdist:
    wheel_metadata = email.message_from_bytes(wheel.read(next(
        name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
    )))
    assert wheel_metadata["Name"] == metadata["name"]
    assert wheel_metadata["Version"] == metadata["version"]
    assert wheel_metadata["License-Expression"] == metadata["license"]
    assert wheel_metadata.get_all("Requires-Dist") == metadata["dependencies"]
    for filename in ("LICENSE", "NOTICE"):
        wheel_path = next(name for name in wheel.namelist() if name.endswith(f"/licenses/{filename}"))
        sdist_path = next(name for name in sdist.getnames() if name.endswith(f"/{filename}"))
        expected = (project / filename).read_bytes()
        assert wheel.read(wheel_path) == expected
        assert sdist.extractfile(sdist_path).read() == expected
print("Wheel metadata and wheel/sdist LICENSE + NOTICE checks passed")
PY

# Install only built wheels plus their real dependencies into a second, fresh venv.
uv venv --python "$python_version" "$work_root/wheel-venv"
uv pip install --python "$work_root/wheel-venv/bin/python" \
  "$work_root/wheelhouse/"*.whl "$work_root/dist/"*.whl
uv pip check --python "$work_root/wheel-venv/bin/python"
mkdir "$work_root/smoke"
cd "$work_root/smoke"
"$work_root/wheel-venv/bin/python" -I - <<'PY'
import json
import sys
from importlib.metadata import distribution, entry_points, version
from pathlib import Path

import jsonschema
import pydantic
import toolframe_sdk
import toolframe_storage_sqlite
from toolframe_storage_sqlite.plugin import SqliteProvider

for module in (toolframe_sdk, toolframe_storage_sqlite, pydantic, jsonschema):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    print(f"Installed import: {module.__name__} -> {module.__file__}")
for package in ("toolframe-sdk", "toolframe-storage-sqlite"):
    direct_url = json.loads(distribution(package).read_text("direct_url.json") or "{}")
    assert not direct_url.get("dir_info", {}).get("editable", False)
    print(f"Installed distribution: {package}=={version(package)}")
plugins = entry_points(group="toolframe.plugins", name="storage-sqlite")
assert len(plugins) == 1
assert next(iter(plugins)).load().factory.endswith(":" + SqliteProvider.__name__)
print("Installed wheel imports and plugin entry point passed")
PY

# Exercise the actual storage suite against the installed wheel, outside its source tree.
uv export --project "$project" --locked --only-group dev --no-hashes \
  --output-file "$work_root/dev-requirements.txt" > /dev/null
uv pip install --python "$work_root/wheel-venv/bin/python" -r "$work_root/dev-requirements.txt"
cp -R "$project/tests" "$work_root/smoke/tests"
"$work_root/wheel-venv/bin/python" -I -m pytest -q -c "$project/pyproject.toml" \
  --import-mode=importlib "$work_root/smoke/tests"
uv pip check --python "$work_root/wheel-venv/bin/python"

# Retain only the validated provider distributions, never the dependency wheelhouse.
cp "$work_root/dist/"*.whl "$work_root/dist/"*.tar.gz "$output/"
echo "Validation passed. Distributions: $output"
