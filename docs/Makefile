# Minimal makefile for Sphinx documentation

# You can set these variables from the command line, and also
# from the environment for the first two.
SPHINXOPTS    ?=
# `python -m sphinx`, not the `sphinx-build` console script: under `uv run` the
# module form is what resolves inside the managed environment.
SPHINXBUILD   ?= python -m sphinx
# `.`, not `source`. docs/source/ is a second, abandoned doc tree -- its own
# conf.py, its own index.rst, and a toctree naming ten user/ pages of which two
# exist. The live sources (and the conf.py that is actually maintained) are here
# in docs/, so pointing at source/ meant every target built the wrong tree.
SOURCEDIR     = .
BUILDDIR      = _build

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help html pdf clean linkcheck Makefile

# Generate HTML documentation
html:
	@mkdir -p $(BUILDDIR)/html
	$(SPHINXBUILD) -b html "$(SOURCEDIR)" "$(BUILDDIR)/html" $(SPHINXOPTS) $(O)
	@echo
	@echo "Build finished. The HTML pages are in $(BUILDDIR)/html."

# Generate PDF documentation
pdf:
	@mkdir -p $(BUILDDIR)/pdf
	$(SPHINXBUILD) -b latex "$(SOURCEDIR)" "$(BUILDDIR)/latex" $(SPHINXOPTS) $(O)
	@cd $(BUILDDIR)/latex && make
	@echo
	@echo "Build finished. The PDF file is in $(BUILDDIR)/latex."

# Clean build directory
clean:
	rm -rf $(BUILDDIR)/*

# Link check
linkcheck:
	@$(SPHINXBUILD) -b linkcheck "$(SOURCEDIR)" "$(BUILDDIR)/linkcheck" $(SPHINXOPTS) $(O)
	@echo
	@echo "Link check complete; look for any errors in the above output " \
	      "or in $(BUILDDIR)/linkcheck/output.txt."

# Catch-all target: route all unknown targets to Sphinx.
#
# This file was named `makefile` (lowercase) while the rule read `%: Makefile`.
# On a case-insensitive filesystem those are the same file, so `make html` matched
# the catch-all with $@ = "makefile" -- and it did so *before* the explicit `html`
# rule, because a pattern rule that can remake the makefile itself is tried first.
# The result was `sphinx -M makefile`, i.e.
# `SphinxError: Builder name makefile not registered`. The rename to `Makefile`
# plus the .PHONY entries above stop make trying to rebuild it.
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
