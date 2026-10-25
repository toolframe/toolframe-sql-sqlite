# Releasing toolframe-sql-sqlite

The distribution name is `toolframe-sql-sqlite`. The version is defined in `pyproject.toml`.
Release tags must exactly match `v<project.version>`. Ordinary CI never uploads
packages. The manual `publish.yml` workflow reruns validation and publishes only
artifacts produced by that successful run.

## Validate

Run the repository's existing validation workflow; its local commands and
integration coverage are described in [CI](ci.md).

Publish any newly required SDK version before the plugin. Compatible Core or SDK
updates alone do not require a plugin release. The coordinated order and native CLI channels are documented in
[Toolframe's release guide](https://github.com/toolframe/toolframe-core/blob/main/docs/releasing.md).

## Trusted Publishers

Create a GitHub environment named `pypi`. On PyPI, configure a normal or
pending Trusted Publisher with these values:

| Field | Value |
| --- | --- |
| Project | `toolframe-sql-sqlite` |
| GitHub owner | `toolframe` |
| GitHub repository | `toolframe-sql-sqlite` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

If publishing from another owner, use that actual owner in the publisher binding.
Follow [PyPI's Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
Only the publishing job receives `id-token: write`; no PyPI API-token secret is
needed. Configure environment reviewers and release-tag protections as appropriate.

Jobs that fetch sibling repositories support the optional read-only
`TOOLFRAME_READ_TOKEN` GitHub secret. It must be able to read those repositories
while they are private. Public siblings require no extra token; secrets are not
available to untrusted fork PRs. `TOOLFRAME_REPOSITORY_OWNER` overrides the trusted
sibling owner where the workflow exposes that variable.

## Publish and verify

After the release commit and matching tag are available remotely, manually run
`publish.yml` at the matching `v<project.version>` tag to publish to PyPI.
A branch or mismatched tag is rejected. Downloaded files are the artifacts from
that run; the publishing job does not rebuild them. Published files cannot be
replaced under the same version.

After PyPI publication, verify a clean installation:

```sh
uv venv --python 3.14.7 build/consumer
uv pip install --python build/consumer/bin/python 'toolframe-sql-sqlite==0.4.1'
uv pip check --python build/consumer/bin/python
```

Test the specific artifact with dependencies from PyPI. Local artifact checks
alone do not demonstrate that the published package resolves correctly from
PyPI. Keep logs, hashes and exact revisions in CI artifacts or ignored local
evidence directories.
