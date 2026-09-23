# CI and publishing

## Local validation

Install Python 3.14.7, uv 0.12.8, Git and Bash. Keep a readable sibling
`../toolframe-sdk` Git checkout containing commit
`121e60eb5ba77717a382cd474c7a31d2de086d13`. Then run:

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

`ci.yml` runs for pull requests, pushes and manual dispatches, and is reusable by
`publish.yml`. It runs the same script on `ubuntu-24.04` with Python 3.14.7 and
uv 0.12.8, retaining `python-distributions` for seven days after successful checks.
All actions are pinned to full commit IDs. Default permissions are `contents: read`;
checkout credentials are not persisted. The reusable workflow has no concurrency
group that could cancel its caller.

Before remote CI can run, push this repository and the pinned SDK commit to their
GitHub repositories. The SDK repository name is `toolframe-sdk`; its owner defaults
to `github.repository_owner`. Set the repository variable
`TOOLFRAME_REPOSITORY_OWNER` when the SDK has a different trusted owner. A fork PR
uses the base repository's owner/variable, never the contributor's fork owner.
This checkout configuration expects a publicly readable SDK repository. A private
SDK needs a separately designed read-only cross-repository credential policy;
the default `GITHUB_TOKEN` cannot read arbitrary private siblings, and fork PRs
must not receive repository secrets.

## Publishing setup and use

Publishing is manual only. `publish.yml` offers `testpypi` (default) and `pypi`.
The selected run ref must be exactly `refs/tags/v<project.version>`; the current
version requires `v0.2.0`. A branch run or mismatched tag fails before validation.
The reusable CI validates the same run revision, and the publishing job downloads
its exact artifact without rebuilding. Only that job receives `id-token: write`.
No PR run uploads packages, and no API token is needed.

Before the first upload:

1. Create GitHub environments named `testpypi` and `pypi`, with release-tag
   restrictions and any required reviewers appropriate to the repository.
2. On **each** registry, configure a Trusted Publisher (or pending publisher)
   for project `toolframe-storage-sqlite`: the actual GitHub owner, repository
   `toolframe-storage-sqlite`, workflow filename `publish.yml`, and environment
   `testpypi` or `pypi` respectively. TestPyPI and PyPI are separate services.
3. Make `toolframe-sdk==0.2.0` available to consumers on their chosen index before
   expecting normal installation to succeed. CI's temporary SDK wheel is not
   uploaded with this provider. Production PyPI consumers need the SDK on PyPI;
   TestPyPI may lack runtime dependencies, so test installation must supply them
   deliberately, as the local validation does with the pinned SDK wheel and PyPI.
4. Commit the intended release, create and push its matching version tag, then
   dispatch **Publish Python package** on that tag with the desired registry.
   For example, a future operator can run
   `gh workflow run publish.yml --ref v0.2.0 -f registry=testpypi`.
   After checking TestPyPI, dispatch on the same tag with `registry=pypi`.

Duplicate uploads fail rather than being silently skipped. Publishing files and
configuration does not perform deployment: no tag push, registry registration or
package upload is part of local validation.

References: [uv CI integration](https://docs.astral.sh/uv/guides/integration/github/),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/),
[PyPA publish action](https://github.com/pypa/gh-action-pypi-publish), and
[GitHub runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
