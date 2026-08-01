# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import os
import sys

# Add the project root directory to the Python path
sys.path.insert(0, os.path.abspath(".."))

project = "Parsl AWS Provider"
copyright = "2025-2026, Scott Friedman and Project Contributors"
author = "Scott Friedman and Project Contributors"

# Read the version from the installed package rather than restating it. This was
# pinned at "0.1.0" through five releases, because a hardcoded literal here has
# nothing to keep it honest -- bump-my-version does not know about it.
try:
    from importlib.metadata import version as _pkg_version

    release = _pkg_version("parsl-aws-provider")
except Exception:  # pragma: no cover - docs build without the package installed
    release = "unknown"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.autosummary",
    "sphinx_rtd_theme",
    "sphinx.ext.githubpages",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    ".venv",
    "venv",
    # Notes for people editing the docs, not a page of the built site.
    "README.md",
]

# Warnings are errors. Every page here is now checked against the real API, so a
# new warning means a genuine break -- a dead cross-reference, a missing include,
# a malformed table. The build carried 398 of them before #124, which is what let
# ~65 toctree entries name pages nobody had written.
nitpicky = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "parsl": ("https://parsl.readthedocs.io/en/stable/", None),
    "boto3": ("https://boto3.amazonaws.com/v1/documentation/api/latest/", None),
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "sphinx_rtd_theme"

# No html_static_path/html_logo/html_favicon: `_static/` does not exist, and
# naming a logo and favicon inside it produced three warnings on every build for
# files that were never added.

# -- Napoleon settings ------------------------------------------------------
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = False
napoleon_type_aliases = None
napoleon_attr_annotations = True

# -- MyST Parser settings ---------------------------------------------------
myst_enable_extensions = [
    "colon_fence",
    "deflist",
]
myst_heading_anchors = 3

# -- Autodoc settings -------------------------------------------------------
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
autoclass_content = "both"

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
