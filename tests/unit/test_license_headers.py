"""Keep the SPDX headers accurate and present.

The copyright range said "2025" for the whole of 2026 and was only noticed when
someone read a header (#119). A grep is cheaper than rediscovering it next year,
so the range lives here rather than in a reviewer's memory.

Files are enumerated from ``git ls-files``, so anything untracked — build output,
a scratch script, ``.venv`` — is out of scope by construction.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import subprocess
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_COPYRIGHT = (
    "SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors"
)
EXPECTED_LICENSE = "SPDX-License-Identifier: Apache-2.0"

# Files that legitimately carry no header: data, generated lockfiles, and the
# licence text itself (which has its own header in the Apache appendix, checked
# separately below).
_EXEMPT_NAMES = {
    "uv.lock",
    "LICENSE",
    "py.typed",
    ".python-version",
    "CHANGELOG.md",
    "README.md",
}
_EXEMPT_SUFFIXES = {
    ".json",
    ".txt",
    ".cfg",
    ".ini",
    ".svg",
    ".png",
    ".ico",
    ".zip",
    ".lock",
}
_EXEMPT_DIRS = {"archive", ".github/ISSUE_TEMPLATE"}

# tools/ is one-off debug scripts, 90 of which have never carried a header. #93
# prunes the directory in v0.8.0, so requiring headers there now would mean
# annotating files that are about to be deleted. The stale-year checks below still
# cover tools/ — this exemption is only about *missing* headers. Ruff and bandit
# are scoped the same way, for the same reason.
_HEADER_EXEMPT_DIRS = _EXEMPT_DIRS | {"tools"}

# This file has to be exempt from the two content scans below: it necessarily
# contains the stale form as a search literal, so scanning itself would report a
# permanent failure. Its own header is still checked, by the parametrized
# test_source_file_has_both_spdx_tags — that one looks for the *expected* form
# rather than the forbidden one, so it works fine here.
_SELF = Path(__file__).resolve()


def _tracked_files() -> List[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def _source_files() -> List[Path]:
    """Tracked Python and shell sources, where a header is non-negotiable."""
    return sorted(
        p
        for p in _tracked_files()
        if p.suffix in (".py", ".sh")
        and p.is_file()
        and not any(
            str(p.relative_to(REPO_ROOT)).startswith(d) for d in _HEADER_EXEMPT_DIRS
        )
    )


def test_no_file_carries_a_stale_copyright_year():
    """No tracked file still says 2025 alone.

    This is the check that makes #119 stay fixed. When the range needs extending,
    update EXPECTED_COPYRIGHT and re-run the sweep; the failure list is the
    worklist.
    """
    stale = []
    for path in _tracked_files():
        if not path.is_file() or path.suffix in _EXEMPT_SUFFIXES:
            continue
        if path.resolve() == _SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "SPDX-FileCopyrightText:" not in text:
            continue
        if "SPDX-FileCopyrightText: 2025 Scott Friedman" in text:
            stale.append(str(path.relative_to(REPO_ROOT)))
    assert not stale, (
        f"Copyright range is stale in {len(stale)} file(s); expected "
        f"{EXPECTED_COPYRIGHT!r}:\n" + "\n".join(sorted(stale))
    )


def test_only_one_copyright_form_exists():
    """Every header that exists uses the same wording.

    Divergent forms are how the range drifts in the first place — one file gets
    updated by hand and the rest do not.
    """
    wrong = []
    for path in _tracked_files():
        if not path.is_file() or path.suffix in _EXEMPT_SUFFIXES:
            continue
        if path.resolve() == _SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            if "SPDX-FileCopyrightText:" in line and EXPECTED_COPYRIGHT not in line:
                wrong.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()}")
    assert not wrong, "Unexpected copyright forms:\n" + "\n".join(wrong)


@pytest.mark.parametrize(
    "path", _source_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_source_file_has_both_spdx_tags(path: Path):
    """Every tracked Python and shell source carries licence and copyright.

    An empty ``__init__.py`` is exempt — there is nothing in it to license.
    """
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        pytest.skip("empty file")
    assert EXPECTED_LICENSE in text, f"missing {EXPECTED_LICENSE!r}"
    assert EXPECTED_COPYRIGHT in text, f"missing {EXPECTED_COPYRIGHT!r}"


def test_license_appendix_carries_the_header():
    """LICENSE's Apache appendix names the same range as everything else."""
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert EXPECTED_COPYRIGHT in text
