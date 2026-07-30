# Parsl Ephemeral AWS Provider Tests

This directory contains the test suite for the Parsl Ephemeral AWS Provider.

Everything runs through `uv`, per `CLAUDE.md` — never bare `pytest`, which resolves
against whatever happens to be on `PATH` rather than `.venv`.

## Test Structure

- `unit/` — individual components in isolation, with moto and `unittest.mock`. No
  network, no emulator.
- `security/` — pure-mock tests of the credential, audit, and policy layers. Marked
  `unit`, and run alongside `unit/` in CI as one coverage-gated job.
- `integration/` — interactions between components. A subset is pure-moto; the rest
  needs the substrate emulator and skips without it.
- `aws/` — real-AWS E2E tests, marked `@pytest.mark.aws`. These create billable
  resources.
- `test_substrate_emulation.py` — conformance tests for the emulator itself. Drives
  raw boto3 and imports no package code, so a failure here means the emulator
  regressed rather than the provider.
- `substrate_support.py` — emulator session/client helpers and VPC setup/teardown.
- `support.py`, `conftest.py` — shared fixtures.

## Running Tests

### Unit and security tests

```bash
make test-unit          # both directories, with the 65% coverage gate CI applies

uv run pytest tests/unit/test_standard_mode.py -v --no-cov   # one file
```

### Integration tests

These run against [substrate](https://github.com/scttfrdmn/substrate), a local AWS
emulator. It replaced LocalStack in #125 — LocalStack OSS is end-of-life. Substrate
is a container image, so there is no Python package to install:

```bash
make test-integration   # starts substrate via docker-compose.substrate.yml, then runs
```

Or manage the emulator yourself:

```bash
make substrate-up
uv run pytest tests/integration -v
make substrate-reset    # wipe emulator state between runs
make substrate-down
```

Without a running emulator the emulator-gated tests skip and the moto-backed ones
still run. See [`docs/substrate_testing.md`](../docs/substrate_testing.md) for the
known fidelity gaps and how to point the suite at a different port.

### Real-AWS E2E tests

```bash
export AWS_PROFILE=aws AWS_TEST_REGION=us-east-1
export AWS_TEST_VPC_ID=vpc-… AWS_TEST_SUBNET_ID=subnet-… AWS_TEST_SG_ID=sg-…
make test-aws           # prompts before creating billable resources
```

`tests/aws/conftest.py` skips when those three IDs are unset, so a run without them
is a no-op rather than a failure.

### Code coverage

```bash
make coverage           # everything except the real-AWS tests, HTML report in htmlcov/
```

## Markers

A marker only *selects* tests — it never skips them. An emulator-backed file must
pair its markers with a `skipif(not is_substrate_available())` guard; without one, a
plain `pytest tests/integration` errors instead of skipping.

| Marker | Meaning |
|---|---|
| `unit` | Isolated, mocked, no external services |
| `integration` | Cross-component; may or may not need an endpoint |
| `substrate` | Requires a running substrate emulator |
| `aws` | Requires real AWS credentials; costs money |
| `slow` | Long-running |

## Writing Tests

1. **Unit tests** — one function or class at a time, mocking external dependencies.
   Cover error handling, not just the happy path.
2. **Integration tests** — realistic workflows across components, against the
   emulator. Clean up what you create.
3. **Unique resource names** — emulator state persists for the server process's
   lifetime, so a fixed name collides with `ResourceConflictException` on a second
   run. Suffix with `uuid.uuid4().hex[:8]`.
4. **Never reach real AWS by accident** — `conftest.py` injects synthetic
   credentials into every test lacking `@pytest.mark.aws` and drops `AWS_PROFILE`,
   so an unmocked call fails as an auth error against a fake account rather than
   mutating a real one.

## CI Pipeline

`.github/workflows/ci.yml` runs: ruff lint and format checks, bandit, mypy
(reported, not gated), unit + security tests across Python 3.10–3.12 with a 65%
coverage gate, integration tests against a pinned substrate service container, the
emulator conformance suite (gated), bats shell tests, a package build, and a docs
build. Real-AWS E2E is manual dispatch only.

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [substrate](https://github.com/scttfrdmn/substrate)
- [moto Documentation](https://docs.getmoto.org/)

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
