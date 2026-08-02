# CI/CD Pipeline

This document describes the continuous integration and release pipeline for the
Parsl Ephemeral AWS Provider.

The authoritative definitions are the workflow files themselves —
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and
[`.github/workflows/release.yml`](../.github/workflows/release.yml). This page
explains *why* each job exists and how to run the same checks locally; it
deliberately does not inline the YAML, because the copy that used to live here
drifted out of sync with the workflow it documented.

## Two workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci.yml` | push to `main`, pull requests, manual dispatch | lint, type-check, tests, build, docs |
| `release.yml` | push of a `v*` tag | version check, tests, build, GitHub release, PyPI |

`ci.yml` replaced a near-duplicate `ci.yml` + `ci-cd.yml` pair that between them
ran the same unit suite five times, over Python versions the package no longer
supports. Everything now runs through `uv` — `uv sync --locked` installs from the
committed `uv.lock`, so CI resolves the same dependency set a developer's
checkout does.

## `ci.yml` jobs

### `lint`

`ruff check` and `ruff format --check`, plus a `bandit` security scan.

Two details worth knowing:

* **Scope is `parsl_aws_provider tests`, not `.`** — `tools/` carries pre-existing
  errors (bare excepts, unused imports) in one-off debug scripts that
  [#93](https://github.com/scttfrdmn/parsl-aws-provider/issues/93) prunes in
  v0.8.0. A `.`-scoped check could never pass.
* **ruff-format, not black.** `black` and `isort` are no longer dependencies. The
  repo formats with `ruff-format` at its default 88-character line length; the old
  `[tool.black] line-length = 100` disagreed with how every file was actually
  formatted, so running black reformatted ~80 unrelated lines. The
  `ruff-pre-commit` hook is pinned to the same ruff version `uv.lock` resolves, so
  a local pre-commit pass and the CI check cannot demand opposite output.

### `type-check`

`mypy parsl_aws_provider`, reported but **not gated** (`continue-on-error: true`)
while the pre-existing error count is worked down under
[#81](https://github.com/scttfrdmn/parsl-aws-provider/issues/81) and
[#82](https://github.com/scttfrdmn/parsl-aws-provider/issues/82). Remove the
`continue-on-error` once it reaches zero.

### `unit-tests`

Runs `tests/unit` and `tests/security` on Python 3.10, 3.11, and 3.12.
`requires-python` is `>=3.10` — Parsl 2026.x dropped 3.9, so the old 3.8/3.9
matrix entries could not have installed the package at all.

`tests/security` is pure-mock and marked `unit`, so it belongs to this job rather
than a separate one.

**Selection is by path, not `-m unit`.** That distinction caused a long-running
divergence: the Makefile selected `-m unit`, which collected 88 of 295 tests and
passed, while CI ran the unmarked set and failed. Selecting by path means a
newly-added file without a marker still runs.

This job carries the project's real coverage gate, `--cov-fail-under=65`. The
`--cov-fail-under` in `pyproject.toml` is a lower smoke floor because `addopts`
applies to *every* invocation, including narrow ones — `pytest tests/integration`
alone measures 34%.

### `integration-tests`

Runs `tests/integration` against a pinned substrate service container
(`ghcr.io/scttfrdmn/substrate`), plus a gated `tests/test_substrate_emulation.py`
conformance step. Substrate replaced LocalStack in
[#125](https://github.com/scttfrdmn/parsl-aws-provider/issues/125): LocalStack OSS
is end-of-life and its `latest` community tag now resolves to the Pro build, which
exits 55 without a license token -- before any step runs, and outside what
`continue-on-error` covers.

The integration suite itself is **not gated** (`continue-on-error: true`): now that
an endpoint is present these tests actually execute rather than skipping, and 46 mode
constructions across 9 files still omit the network IDs
[#69](https://github.com/scttfrdmn/parsl-aws-provider/issues/69) made required
([#92](https://github.com/scttfrdmn/parsl-aws-provider/issues/92), v0.8.0). Gating
before that lands would make every PR red on known debt. The conformance step *is*
gated, because it drives raw boto3 and cannot be tripped by that test-side debt.

Note that a pytest marker only *selects* tests — it never skips them. Each
emulator-backed file pairs its markers with a `skipif(not is_substrate_available())`
guard; without one, a plain `pytest tests/integration` errors instead of skipping.

### `aws-e2e-tests`

The real-AWS E2E suite (`tests/aws`), **manual dispatch only** — it bills money
and needs a pre-provisioned VPC, subnet, and security group.
[#60](https://github.com/scttfrdmn/parsl-aws-provider/issues/60) closed with 51
tests here that no workflow referenced.

Credentials come from OIDC via `aws-actions/configure-aws-credentials`, so no
long-lived access keys live in secrets. Configure:

* **Secret** `AWS_E2E_ROLE_ARN` — the role the workflow assumes.
* **Variables** `AWS_TEST_REGION`, `AWS_TEST_VPC_ID`, `AWS_TEST_SUBNET_ID`,
  `AWS_TEST_SG_ID`.

The job is gated on `vars.AWS_TEST_REGION != ''`, so a dispatch on a repository
without these configured skips rather than failing. The gate is what makes that
true, not `tests/aws/conftest.py`: conftest does skip when the three IDs are
unset, but pytest is never reached — `configure-aws-credentials` fails first on
the empty region, so before
[#161](https://github.com/scttfrdmn/parsl-aws-provider/issues/161) every dispatch
was red.

Once it does run, conftest validates the IDs against `AWS_TEST_REGION` up front —
IDs from another region otherwise surface minutes in, from deep inside
`RunInstances`, after instances have been billed. Pick a subnet in an AZ that
offers your instance type (`us-east-1e` does not offer `t3.micro`).

A final `always()` step runs `parsl-aws-cleanup --dry-run` to report
orphans, since a failed test is exactly when instances are most likely to be left
running. It reports without deleting, so CI never mutates a shared account. The
script takes credentials from the boto3 chain — the OIDC credentials the earlier
step exported — rather than a named profile; it previously defaulted to the local
`aws` profile and died here on "The config profile (aws) could not be found".

### `test-bats`

Runs `bats tests/bats/` for the shell scripts under `scripts/`.

### `build`

`uv build`, then `twine check`. Also asserts the CloudFormation templates are
present in the built wheel: before
[#112](https://github.com/scttfrdmn/parsl-aws-provider/issues/112),
`get_cf_template()` resolved templates by filesystem path, so a wheel that omitted
them failed only at runtime, on a real AWS call.

### `docs`

Builds the Sphinx documentation and uploads it as an artifact.

## `release.yml`

Triggered by pushing a `v*` tag. Before anything else it **verifies the tag
matches `parsl_aws_provider.__version__`** — `bump-my-version` has silently
missed `__init__.py` before (its `[tool.bumpversion]` search string drifted out of
sync), and v0.6.0 shipped with `__version__ == "0.1.0"`. Catching that here
matters because a PyPI version can never be reused. `make version-verify` runs the
same check locally, and the `version-bump-*` targets call it automatically.

PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/)
via OIDC, so no `PYPI_API_TOKEN` secret is needed. The GitHub release is created
with `gh release create`; `actions/create-release` and
`actions/upload-release-asset` are both archived and unmaintained.

The old `ci-cd.yml` carried a second `build-and-publish` job on
`release: published`, so a tag push followed by a published release ran two
independent PyPI uploads. This is now the only publish path.

## Running the same checks locally

The Makefile runs exactly what CI runs, through `uv`:

```bash
make lint-python      # ruff check + ruff format --check
make type-check       # mypy
make test-unit        # tests/unit + tests/security, with the 65% gate
make test-integration # starts substrate, then tests/integration
make test-aws         # tests/aws against real AWS (prompts first; costs money)
make build            # uv build
make version-verify   # pyproject and __init__ versions agree
```

Never invoke `pytest`, `ruff`, or `mypy` bare — they resolve against whatever is
on `PATH` rather than `.venv`. Use `uv run` (or the Makefile, which does).

## Badges

```markdown
[![CI](https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml/badge.svg)](https://github.com/scttfrdmn/parsl-aws-provider/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/scttfrdmn/parsl-aws-provider/branch/main/graph/badge.svg)](https://codecov.io/gh/scttfrdmn/parsl-aws-provider)
[![PyPI version](https://badge.fury.io/py/parsl-aws-provider.svg)](https://badge.fury.io/py/parsl-aws-provider)
```

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
