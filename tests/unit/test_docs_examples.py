"""Validate every configuration in the docs and examples against the real code.

The provider rejects unknown keyword arguments (#105), so a stale option in a
document or example is a crash at construction rather than something silently
ignored. Phase 8 found that most of both trees had drifted that far: docs
recommended options that never existed, and examples passed mode *objects* and
state-store *instances* to a provider that takes strings.

Rather than re-audit by hand, these tests parse the real signatures out of the
package and check every call site in ``docs/*.md``, ``examples/*.py``, and the
top-level READMEs against them. Nothing here touches AWS or constructs a
provider — it is all AST inspection, so it runs in milliseconds.

What is deliberately *not* checked: whether a configuration is sensible, or
whether the AWS resources it names exist. Only that the options are real.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import ast
import importlib
import importlib.util
import inspect
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

from parsl.config import Config
from parsl.executors import HighThroughputExecutor

from parsl_aws_provider import EphemeralAWSProvider, GlobusComputeProvider

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
EXAMPLES_DIR = REPO_ROOT / "examples"

# Classes whose keyword arguments we check, resolved by the name used at the call
# site. Both Parsl classes are included because a doc that gets the provider right
# and `max_workers` wrong is still a doc that does not run.
CHECKED_CALLABLES = {
    "EphemeralAWSProvider": EphemeralAWSProvider,
    "GlobusComputeProvider": GlobusComputeProvider,
    "HighThroughputExecutor": HighThroughputExecutor,
    "Config": Config,
}

# Markdown fence, capturing the language token.
_FENCE = re.compile(r"^```(\w*)\s*$")

# Fences whose contents are Python and should parse.
_PYTHON_LANGS = {"python", "py"}

# Classes whose *construction* has to be traceable to something importable. The
# kwarg check above can only inspect names it recognises, so it said nothing at
# all about a call to a class that does not exist -- see
# test_documented_provider_classes_resolve.
_CONSTRUCTED_CLASS = re.compile(r"(Provider|Executor)$")

# Modules a reader is expected to install themselves, imported inside app bodies
# to show what a worker needs rather than what the driver does. Anything else
# must resolve in this environment.
_READER_INSTALLED_MODULES = {"numpy", "scipy", "pandas", "sklearn"}


def _own_kwargs(cls) -> Set[str]:
    """Keyword names declared by one class's own __init__."""
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - C-level __init__
        return set()
    return {
        name
        for name, p in params.items()
        if name != "self" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }


def _accepted_kwargs(target) -> Set[str]:
    """Every keyword argument a class really accepts, walking the MRO.

    ``**kwargs`` is *not* treated as "accepts anything" here, because on this
    provider it is the opposite: it exists only so unknown options can be caught
    and rejected with a useful message (#105). But `GlobusComputeProvider` does
    forward through it to the base, so the accepted set is the union over the MRO.
    """
    return set().union(*(_own_kwargs(cls) for cls in inspect.getmro(target)))


def _known_classes() -> Dict[str, type]:
    """Every provider/executor class importable from the packages the docs use.

    A doc fragment often constructs `EphemeralAWSProvider(...)` without repeating
    the import, because the surrounding prose established it. That is fine to
    read, so an unbound name is resolved against this registry rather than
    treated as a failure — but a name in *neither* the block's imports nor here is
    fiction.
    """
    registry: Dict[str, type] = {}
    for module_name in (
        "parsl_aws_provider",
        "parsl.executors",
        "parsl.providers",
        "parsl.config",
    ):
        module = importlib.import_module(module_name)
        for name in dir(module):
            attr = getattr(module, name)
            if not name.startswith("_") and isinstance(attr, type):
                registry.setdefault(name, attr)
    return registry


def _iter_python_blocks(path: Path):
    """Yield (start_line, source) for every Python fenced block in a markdown file.

    ``start_line`` is 1-indexed and points at the first line of code, so a failure
    message lands on something clickable.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        match = _FENCE.match(lines[i])
        if not match:
            i += 1
            continue
        lang, start = match.group(1), i + 1
        end = start
        while end < len(lines) and not _FENCE.match(lines[end]):
            end += 1
        if lang in _PYTHON_LANGS:
            yield start + 1, "\n".join(lines[start:end])
        i = end + 1


def _markdown_files() -> List[Path]:
    """Every markdown file whose Python blocks are meant to be runnable."""
    files = sorted(DOCS_DIR.glob("*.md"))
    files.append(REPO_ROOT / "README.md")
    files.append(EXAMPLES_DIR / "README.md")
    return [f for f in files if f.is_file()]


def _example_files() -> List[Path]:
    return sorted(EXAMPLES_DIR.glob("*.py"))


def _bad_kwargs(source: str) -> List[Tuple[int, str, str]]:
    """Find keyword arguments no checked callable accepts.

    Returns (relative line, callable name, keyword) triples. A call to something
    not in ``CHECKED_CALLABLES`` is ignored, as is a ``**spread`` whose keys cannot
    be resolved statically.
    """
    findings = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `Foo(...)` and `mod.Foo(...)` alike.
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        else:
            continue
        target = CHECKED_CALLABLES.get(name) or _known_classes().get(name)
        if target is None:
            # Still skipped, but no longer the whole story: a fictional class name
            # used to end here silently, which is how the README's
            # `AWSProvider(enable_ssm_tunneling=...)` survived (#197).
            # test_documented_provider_classes_resolve now fails on those.
            continue
        accepted = _accepted_kwargs(target)
        for kw in node.keywords:
            if kw.arg is None:  # **spread — cannot resolve statically
                continue
            if kw.arg not in accepted:
                findings.append((node.lineno, name, kw.arg))
    return findings


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_doc_python_blocks_parse(path: Path):
    """Every ```python block in the docs is syntactically valid Python.

    A block that does not parse cannot possibly run, and readers copy these.
    Signatures and pseudocode belong in a ```text fence instead.
    """
    failures = []
    for lineno, source in _iter_python_blocks(path):
        try:
            ast.parse(source)
        except SyntaxError as exc:
            failures.append(
                f"{path.name}:{lineno}: {exc.msg} (block line {exc.lineno})"
            )
    assert not failures, "Unparseable Python blocks:\n" + "\n".join(failures)


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_doc_configurations_use_real_options(path: Path):
    """No doc passes a keyword argument the real signature rejects.

    This is the check that would have caught the pre-v0.7.0 drift — `use_ssh_tunnel`,
    `max_cost_per_hour`, `tags`, `state_store`, `max_workers` and a dozen others
    that were recommended but never implemented.
    """
    failures = []
    for block_line, source in _iter_python_blocks(path):
        try:
            findings = _bad_kwargs(source)
        except SyntaxError:
            continue  # reported by test_doc_python_blocks_parse
        for rel_line, callable_name, kwarg in findings:
            failures.append(
                f"{path.name}:{block_line + rel_line - 1}: "
                f"{callable_name}({kwarg}=...) — not accepted"
            )
    assert not failures, "Options that do not exist:\n" + "\n".join(failures)


def _module_level_imports(source: str):
    """Yield (line, module) for every absolute import anywhere in a block.

    Imports inside a function body are included deliberately: a Parsl app body
    runs on the *worker*, so `import numpy` there is a statement about what
    `worker_init` must install. `_READER_INSTALLED_MODULES` carries those; anything
    else has to resolve here.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split(".")[0]


def _unresolvable_imports(source: str) -> List[Tuple[int, str]]:
    return [
        (line, module)
        for line, module in _module_level_imports(source)
        if module not in _READER_INSTALLED_MODULES
        and importlib.util.find_spec(module) is None
    ]


def _resolve_constructed(source: str) -> List[Tuple[int, str, Optional[type], str]]:
    """Resolve every `SomethingProvider(...)`/`SomethingExecutor(...)` call.

    Returns (line, name, resolved class or None, reason) tuples. Resolution prefers
    an import in the same file, falling back to `_known_classes()`.
    """
    tree = ast.parse(source)
    bindings: Dict[str, str] = {}
    calls: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                bindings.setdefault(alias.asname or alias.name, node.module)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            if name and _CONSTRUCTED_CLASS.search(name):
                calls.append((node.lineno, name))

    registry = _known_classes()
    resolved = []
    for line, name in calls:
        module = bindings.get(name)
        if module is None:
            target = registry.get(name)
            reason = "" if target else "not imported here, and not a known class"
        elif importlib.util.find_spec(module.split(".")[0]) is None:
            target, reason = None, f"imported from {module!r}, which does not exist"
        else:
            target = getattr(importlib.import_module(module), name, None)
            reason = "" if target else f"{module} has no attribute {name!r}"
        resolved.append((line, name, target, reason))
    return resolved


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_documented_imports_resolve(path: Path):
    """Every module a doc imports really exists.

    The gap this closes: the README's quick starts opened
    `from phase15_enhanced import AWSProvider` and
    `from container_executor import ContainerHighThroughputExecutor`. Neither module
    has ever existed in this repository or on PyPI, so both blocks died on line one
    — and because the README is `readme = "README.md"` in pyproject.toml, they were
    also the first code on the PyPI landing page (#197).
    """
    failures = []
    for block_line, source in _iter_python_blocks(path):
        try:
            findings = _unresolvable_imports(source)
        except SyntaxError:
            continue  # reported by test_doc_python_blocks_parse
        for rel_line, module in findings:
            failures.append(
                f"{path.name}:{block_line + rel_line - 1}: "
                f"import {module} — no such module"
            )
    assert not failures, "Imports that cannot resolve:\n" + "\n".join(failures)


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_documented_provider_classes_resolve(path: Path):
    """Every provider/executor a doc constructs is a real class, with real options.

    `test_doc_configurations_use_real_options` could not catch this: it looks each
    call site's name up in `CHECKED_CALLABLES` and *skips* what it does not
    recognise, so a fictional `AWSProvider(enable_ssm_tunneling=True, ...)` was
    passed over in silence while `docs/` stayed clean. That silent skip is why the
    README could rot through six releases of a green suite.
    """
    failures = []
    for block_line, source in _iter_python_blocks(path):
        try:
            resolved = _resolve_constructed(source)
        except SyntaxError:
            continue
        for rel_line, name, target, reason in resolved:
            line = block_line + rel_line - 1
            if target is None:
                failures.append(f"{path.name}:{line}: {name} — {reason}")
    assert not failures, "Classes that do not exist:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_parses(path: Path):
    """Every example is valid Python."""
    ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_uses_real_options(path: Path):
    """No example passes a keyword argument the real signature rejects."""
    findings = _bad_kwargs(path.read_text(encoding="utf-8"))
    failures = [
        f"{path.name}:{line}: {name}({kwarg}=...) — not accepted"
        for line, name, kwarg in findings
    ]
    assert not failures, "Options that do not exist:\n" + "\n".join(failures)


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_passes_mode_as_string(path: Path):
    """`mode=` is a string, never a mode object.

    Every example used to pass `mode=StandardMode(...)`, which raises
    `TypeCheckError`: the provider constructs the mode itself so it can inject the
    session, state store, and resolved AMI.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in (
            "EphemeralAWSProvider",
            "GlobusComputeProvider",
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "mode" and not isinstance(kw.value, ast.Constant):
                failures.append(
                    f"{path.name}:{kw.value.lineno}: mode= is not a literal string"
                )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_disables_htex_encryption(path: Path):
    """Any example building a HighThroughputExecutor sets `encrypted=False`.

    With encryption on, Parsl generates CurveZMQ certificates in the client's
    run_dir, which remote workers cannot read — so they fail to register with no
    useful error. Certificate distribution is #62; until then this is required,
    not optional, and an example that omits it does not work.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "HighThroughputExecutor":
            continue
        encrypted = next((kw for kw in node.keywords if kw.arg == "encrypted"), None)
        if encrypted is None:
            failures.append(f"{path.name}:{node.lineno}: no encrypted= argument")
        elif getattr(encrypted.value, "value", None) is not False:
            failures.append(f"{path.name}:{node.lineno}: encrypted= is not False")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_shuts_the_provider_down(path: Path):
    """Any example that constructs a provider also calls shutdown().

    There is no atexit hook and `parsl.clear()` releases Parsl's resources, not
    AWS ones, so an example that omits this leaves instances billing. detached_mode
    is exempt: preserving the bastion for later adoption is the point of the mode,
    and it prints the command to tear it down.
    """
    if path.name == "detached_mode.py":
        pytest.skip("detached mode deliberately preserves the bastion for adoption")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    constructs = any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None)
        in ("EphemeralAWSProvider", "GlobusComputeProvider")
        for node in ast.walk(tree)
    )
    if not constructs:
        pytest.skip("does not construct a provider")
    assert ".shutdown()" in source, f"{path.name} never calls provider.shutdown()"


# ---------------------------------------------------------------------------
# Cross-checks that keep the docs honest about the code
# ---------------------------------------------------------------------------


def test_network_ids_are_required_parameters():
    """The three network options exist and default to None.

    Every doc asserts the caller must supply them (#69). If a future change gives
    them a default value, the docs are wrong and this fails.
    """
    params = inspect.signature(EphemeralAWSProvider.__init__).parameters
    for name in ("vpc_id", "subnet_id", "security_group_id"):
        assert name in params, f"{name} is no longer a provider option"
        assert params[name].default is None, (
            f"{name} now defaults to {params[name].default!r}"
        )


def test_documented_renames_do_not_resolve():
    """The old names in the troubleshooting rename table really are gone.

    If one is ever reinstated, the table telling readers to stop using it becomes
    misleading.
    """
    params = inspect.signature(EphemeralAWSProvider.__init__).parameters
    removed = [
        "use_spot_instances",
        "spot_max_bid",
        "tags",
        "aws_profile",
        "aws_access_key_id",
        "aws_secret_access_key",
        "state_store",
        "state_bucket",
        "state_file",
        "worker_type",
        "idle_timeout_minutes",
        "create_vpc",
        "use_existing_vpc",
        "max_cost_per_hour",
        "use_ssh_tunnel",
    ]
    resurrected = [name for name in removed if name in params]
    assert not resurrected, (
        "These are documented as removed but now exist — update "
        f"docs/troubleshooting.md: {resurrected}"
    )


def test_the_mode_options_docs_promise_are_reachable():
    """The eight options #136 made reachable are real provider parameters.

    docs/operating_modes.md now documents each in a table as settable, and
    docs/troubleshooting.md says they stopped being unreachable in v0.8.0. Since
    #105 an option absent from the signature is rejected outright, so were one
    dropped the docs would be promising a `ProviderConfigurationError`.
    """
    params = inspect.signature(EphemeralAWSProvider.__init__).parameters
    expected = [
        "idle_timeout",
        "preserve_bastion",
        "bastion_host_type",
        "workflow_id",
        "lambda_runtime",
        "ecs_task_cpu",
        "ecs_task_memory",
        "ecs_container_image",
    ]
    missing = [name for name in expected if name not in params]
    assert not missing, (
        "documented as settable in docs/operating_modes.md but not accepted by "
        f"the constructor: {missing}"
    )


def test_no_default_pins_an_unsupported_python():
    """The Lambda runtime and Fargate image defaults must not name python3.9.

    Both did until #136 — and the ECS one was a *Lambda* base image, whose
    entrypoint is the runtime interface emulator, so Fargate tasks got something
    that expects an invocation event instead of running their command. The
    package requires Python >= 3.10, so a 3.9 default could not run the same code
    as the driver either.
    """
    from parsl_aws_provider.constants import (
        DEFAULT_ECS_CONTAINER_IMAGE,
        DEFAULT_LAMBDA_RUNTIME,
    )

    assert DEFAULT_LAMBDA_RUNTIME >= "python3.10"
    assert "lambda" not in DEFAULT_ECS_CONTAINER_IMAGE
    assert "3.9" not in DEFAULT_ECS_CONTAINER_IMAGE


def test_the_lambda_runtime_default_is_one_cloudformation_allows():
    """A default the template rejects fails the stack, not the call.

    The constant and the template's ``AllowedValues`` are two lists that have to
    agree; nothing else checks them against each other.
    """
    import re

    template = (
        Path(__file__).resolve().parents[2]
        / "parsl_aws_provider"
        / "templates"
        / "cloudformation"
        / "lambda_worker.yml"
    ).read_text(encoding="utf-8")
    from parsl_aws_provider.constants import DEFAULT_LAMBDA_RUNTIME

    allowed = re.findall(r"'(python3\.\d+)'", template)
    assert DEFAULT_LAMBDA_RUNTIME in allowed, (
        f"{DEFAULT_LAMBDA_RUNTIME} is not in the template's AllowedValues: {allowed}"
    )


def test_globus_only_options_are_globus_only():
    """The Globus-specific options are on the subclass, not the base.

    docs/globus_compute.md documents them as the whole of what GlobusComputeProvider
    adds. ``encrypted`` joined them in #138: it configures the engine rather than
    the provider, which is why it does not belong on the base.
    """
    base = _accepted_kwargs(EphemeralAWSProvider)
    globus = _own_kwargs(GlobusComputeProvider)
    assert globus - base == {
        "endpoint_id",
        "container_image",
        "display_name",
        "encrypted",
    }


def test_compute_type_has_no_auto() -> None:
    """`compute_type` accepts ec2/lambda/ecs and nothing else.

    Several docs make the point that there is no provider-level "auto", which is
    only worth saying while it stays true.
    """
    from parsl_aws_provider.provider import ComputeType

    assert {c.value for c in ComputeType} == {"ec2", "lambda", "ecs"}


def test_no_atexit_hook_is_registered() -> None:
    """The docs promise nothing cleans up at interpreter exit; verify that.

    If a hook is ever added, docs/troubleshooting.md and every example's `finally`
    rationale need revisiting — so make that a deliberate change, not a silent one.
    """
    import parsl_aws_provider.provider as provider_module

    source = inspect.getsource(provider_module)
    assert "atexit" not in source, (
        "An atexit hook now exists; update the cost section of "
        "docs/troubleshooting.md and examples/README.md"
    )


def test_default_worker_init_matches_docs() -> None:
    """The documented default worker_init is the real one.

    docs/getting_started.md and docs/troubleshooting.md both describe it as
    installing Python 3.11 and Parsl, which is the basis of the slow-startup advice.
    """
    from parsl_aws_provider.constants import DEFAULT_WORKER_INIT

    assert "python3.11" in DEFAULT_WORKER_INIT
    assert "parsl" in DEFAULT_WORKER_INIT


@pytest.mark.parametrize(
    "doc,anchor",
    [
        ("network-prerequisites.md", "vpc_id"),
        ("troubleshooting.md", "Unknown configuration option"),
        ("spot_fleet.md", "price-capacity-optimized"),
        ("state_persistence.md", "state_store_type"),
        ("globus_compute.md", "worker_init"),
    ],
)
def test_key_docs_exist_and_cover_their_topic(doc: str, anchor: str):
    """The pages other pages link to exist, and still discuss what they promise."""
    path = DOCS_DIR / doc
    assert path.is_file(), f"docs/{doc} is missing but linked from elsewhere"
    assert anchor in path.read_text(encoding="utf-8"), (
        f"docs/{doc} no longer mentions {anchor!r}"
    )


def test_every_example_is_listed_in_the_examples_readme():
    """A new example that nobody links to will not be found."""
    readme = (EXAMPLES_DIR / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in _example_files() if p.name not in readme]
    assert not missing, f"Not listed in examples/README.md: {missing}"
