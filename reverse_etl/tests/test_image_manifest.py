"""The image must contain every script the schedule runs.

WHY THIS TEST EXISTS, and it is the only test here that asserts nothing about
behaviour. The Dockerfile COPYs an explicit allowlist of scripts rather than
`COPY . .`, which keeps tests and caches out of the runtime image. The cost of
that choice is that a new script builds green and then fails at runtime with
"can't open file", and it has now cost us three times:

  * sync_job_roles.py        -- caught, and the Dockerfile comment written
  * sync_skill_terms.py      -- failed daily for three days
  * match_jobs_ai.py         -- same three days

THE SECOND AND THIRD ARE THE INTERESTING ONES. Both have unit tests in this
directory, and both passed the whole time, because a test imports the module
from the repository -- where it has always existed -- and never from the image.
Green tests and a missing file are perfectly compatible, so no behavioural test
could have caught this. Only a test about the PACKAGING can.

WHAT IT DOES NOT DO: build the image, or run docker. It compares the directory
listing against the COPY line, which is the thing that was wrong each time and
is checkable in milliseconds with no daemon.
"""

import re
from pathlib import Path

REVERSE_ETL = Path(__file__).resolve().parents[1]
DOCKERFILE = REVERSE_ETL / "Dockerfile"


def _copied_scripts() -> set[str]:
    """Every *.py named on a COPY line, however the line is wrapped.

    Parsed rather than hard-coded: a list in this file would be a second
    allowlist to forget, which is the bug it is trying to catch.
    """
    text = DOCKERFILE.read_text()
    # Join continuations so a reformatted multi-line COPY still parses.
    text = re.sub(r"\\\s*\n", " ", text)
    found: set[str] = set()
    for line in text.splitlines():
        if not line.strip().upper().startswith("COPY"):
            continue
        found.update(re.findall(r"\b([\w.-]+\.py)\b", line))
    return found


def test_every_top_level_script_is_copied_into_the_image():
    """A script beside the Dockerfile that the image will not contain.

    If this fails, add the filename to the COPY line in reverse_etl/Dockerfile.
    The DAG that runs it would otherwise exit in a few seconds with
    "can't open file '/app/<name>'", which looks like a crash rather than a
    packaging mistake.
    """
    on_disk = {p.name for p in REVERSE_ETL.glob("*.py")}
    missing = sorted(on_disk - _copied_scripts())
    assert not missing, (
        "these scripts sit beside the Dockerfile but are not COPYed into the "
        f"image, so running them fails at runtime: {missing}. Add them to the "
        "COPY line in reverse_etl/Dockerfile."
    )


def test_the_copy_line_names_no_script_that_does_not_exist():
    """The other direction: a renamed or deleted script left on the COPY line.

    `COPY a.py b.py ./` fails the BUILD when b.py is gone, so this turns a
    broken image build into a one-line test failure that names the file.
    """
    on_disk = {p.name for p in REVERSE_ETL.glob("*.py")}
    # requirements.txt and the package directories are copied too; only the
    # top-level scripts are in scope here.
    phantom = sorted(n for n in _copied_scripts() if n not in on_disk)
    assert not phantom, (
        "the Dockerfile COPYs scripts that do not exist, which fails the image "
        f"build: {phantom}"
    )
