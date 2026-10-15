# CI and publishing

## Local validation

Install Python 3.14.7, uv 0.12.8, Git and Bash. Keep a readable sibling
`../toolframe-sdk` Git checkout containing commit
`ef91b609137a5fe7bae8e0c893e74b096ff06ffe`. Then run:

```sh
uv python install 3.14.7
bash scripts/validate.sh /tmp/toolframe-storage-sqlite-validated-dist
```

The output directory must be empty or absent. Use a new directory for another run.
Set `TOOLFRAME_SDK_SOURCE=/path/to/toolframe-sdk` if the sibling is elsewhere;
the required commit is fixed in the script and workflow, regardless of its current
branch or uncommitted changes. Updating that pin requires a reviewed change to both.

The script copies current non-ignored files and exports the pinned SDK into a
temporary workspace. It uses an empty uv cache and fresh virtual environments,
runs `uv sync --locked --group dev`, Ruff and the complete storage suite, and builds
the SDK and provider with `uv build --no-sources`. It runs `twine check --strict`
with [Twine 7.0.0](https://twine.readthedocs.io/en/stable/changelog.html)
(which supports the build backend's metadata 2.5),
checks wheel metadata and both archives' LICENSE/NOTICE bytes, and installs the
built provider and SDK wheels with all runtime dependencies into a new environment.
It verifies imports and plugin discovery from installed packages, then reruns the
storage suite against that wheel outside the source tree. Only the validated
provider wheel and sdist are copied to the output directory. Temporary files are
removed on success or failure. Sibling checkouts and existing `.venv`/`dist` content
are never used as test environments or dependency artifacts.

Static checks, with actionlint 1.7.12 available on `PATH`:

```sh
bash -n scripts/validate.sh
actionlint .github/workflows/ci.yml .github/workflows/publish.yml
```

## GitHub Actions

`ci.yml` runs for pull requests, pushes to `main` and manual dispatches, and is
reusable by `publish.yml`. It runs the same script on `ubuntu-24.04` with
Python 3.14.7 and uv 0.12.8, retaining `python-distributions` for seven days after
successful checks.
All actions are pinned to full commit IDs. Default permissions are `contents: read`;
checkout credentials are not persisted. The reusable workflow has no concurrency
group that could cancel its caller.

Before remote CI can run, push this repository and the pinned SDK commit to their
GitHub repositories. The SDK repository name is `toolframe-sdk`; its owner defaults
to `github.repository_owner`. Set the repository variable
`TOOLFRAME_REPOSITORY_OWNER` when the SDK has a different trusted owner. A fork PR
uses the base repository's owner/variable, never the contributor's fork owner.
Public siblings need no additional token. Private SDK access uses the optional
read-only `TOOLFRAME_READ_TOKEN` described in [releasing](releasing.md). The default
`GITHUB_TOKEN` cannot read private siblings; untrusted fork PRs receive no secrets.

## Publication

See [releasing](releasing.md) for dependency order, private repository access,
Trusted Publisher bindings, version tags and consumer installation checks.
