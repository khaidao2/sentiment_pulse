"""Per-source config for the courses crawler.

Values come from the environment, which in production is injected by the
sent-gen-generated Airflow DAG from the contract YAML
(data-contracts/contracts/producer/courses.yaml) — that YAML is the single
source of truth. The literals below are only dev fallbacks for running locally
without the DAG.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── from the contract's `crawler:` block (DAG injects as UPPER_CASE env) ──
# Comma-separated so a catalogue whose API breaks can be dropped from a run by
# editing the contract, with no code change and no image rebuild.
SOURCES = [s.strip() for s in os.environ.get("SOURCES", "mslearn,mitlearn,freecodecamp").split(",") if s.strip()]
DELAY = float(os.environ.get("DELAY", "0.2"))      # seconds between HTTP calls
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40")) # pagination ceiling per source

# ── from the contract's `kafka:` block ───────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.courses")
SCHEMA_SUBJECT = f"{KAFKA_TOPIC}-value"

# The .avsc ships at a fixed path inside the image, so this is not contract-fed.
SCHEMA_PATH = Path(
    os.environ.get(
        "SCHEMA_PATH",
        _REPO_ROOT / "data-contracts" / "schemas" / "learning" / "courses.avsc",
    )
)
