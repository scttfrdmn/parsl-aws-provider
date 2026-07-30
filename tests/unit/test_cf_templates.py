"""Unit tests for CloudFormation template loading and packaging.

``get_cf_template`` had never been executed by a test: all four of its call
sites patch it out with a string literal, so the fact that it raised
``ModuleNotFoundError`` on every call went unnoticed (#112).

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import tomllib
from pathlib import Path

import pytest

from parsl_ephemeral_aws.utils.aws import get_cf_template

pytestmark = pytest.mark.unit

TEMPLATE_NAMES = [
    "bastion.yml",
    "vpc.yml",
    "lambda_worker.yml",
    "ecs_worker.yml",
    "ec2_worker.yml",
]


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_template_loads(template_name):
    """Every shipped template is loadable and is a CloudFormation document.

    The previous implementation imported ``pkg_resources`` *outside* its own
    ``try``, so once setuptools 81 dropped that module the
    ``except ModuleNotFoundError`` fallback became unreachable and this raised
    unconditionally — taking `DetachedMode.initialize()` with it.
    """
    template = get_cf_template(template_name)

    assert "AWSTemplateFormatVersion" in template
    assert "Resources:" in template


def test_bastion_template_declares_the_output_the_caller_reads():
    """`BastionHostId` must be an output of the bastion stack.

    ``_create_bastion_cloudformation()`` iterates the stack's ``Outputs`` looking
    for exactly this key. The old last-resort fallback returned a placeholder
    template with a lone ``WaitConditionHandle`` and no ``Outputs`` section at
    all, so a missing file surfaced as ``KeyError: 'Outputs'`` several steps
    later instead of as a missing template.
    """
    template = get_cf_template("bastion.yml")

    assert "Outputs:" in template
    assert "BastionHostId" in template


def test_missing_template_raises_rather_than_degrading():
    """An absent template is an error, not a silent placeholder."""
    with pytest.raises(FileNotFoundError, match="no-such-template.yml"):
        get_cf_template("no-such-template.yml")


def test_templates_are_declared_as_package_data():
    """The templates must be packaged, or an installed wheel cannot find them.

    ``[tool.setuptools.packages.find]`` collects modules, not data files. Without
    a ``package-data`` entry the wheel contained only the ``__init__.py`` files
    from the template directories, and this was invisible in development because
    an editable install resolves to the source tree where the filesystem
    fallback succeeds.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        config = tomllib.load(handle)

    patterns = config["tool"]["setuptools"]["package-data"]["parsl_ephemeral_aws"]

    assert "templates/cloudformation/*.yml" in patterns
    # Every template the package loads must be matched by some declared pattern.
    template_dir = (
        Path(__file__).resolve().parents[2]
        / "parsl_ephemeral_aws"
        / "templates"
        / "cloudformation"
    )
    for name in TEMPLATE_NAMES:
        assert (template_dir / name).is_file(), f"{name} is missing from the tree"
