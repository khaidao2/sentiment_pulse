"""Per-source config for the itviec crawler.

Values come from the environment, which in production is injected by the
sent-gen-generated Airflow DAG from the contract YAML
(data-contracts/contracts/producer/itviec.yaml) — that YAML is the single source
of truth. The literals below are only dev fallbacks for running locally without
the DAG.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── from the contract's `crawler:` block (DAG injects as UPPER_CASE env) ──
LISTING_URL = os.environ.get("LISTING_URL", "https://itviec.com/it-jobs")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
DELAY = float(os.environ.get("DELAY", "0.5"))  # seconds between detail fetches

# ── from the contract's `kafka:` block ───────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.itviec")
SCHEMA_SUBJECT = f"{KAFKA_TOPIC}-value"

# The .avsc ships at a fixed path inside the image, so this is not contract-fed.
SCHEMA_PATH = Path(
    os.environ.get(
        "SCHEMA_PATH",
        _REPO_ROOT / "data-contracts" / "schemas" / "raw" / "itviec.avsc",
    )
)
