"""Per-source config for the topcv crawler.

Values come from the environment, which in production is injected by the
sent-gen-generated Airflow DAG from the contract YAML
(data-contracts/contracts/producer/topcv.yaml) — that YAML is the single source
of truth. The literals below are only dev fallbacks for running locally without
the DAG.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── from the contract's `crawler:` block (DAG injects as UPPER_CASE env) ──
LISTING_URL = os.environ.get(
    "LISTING_URL", "https://www.topcv.vn/tim-viec-lam-cong-nghe-thong-tin-cr257"
)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
# TopCV throttles hard: a run at 1.5s took 403 on listing pages and 429 on most
# details, publishing 47 of ~250 listed jobs. Pace is the real fix; MAX_RETRIES
# only recovers the ones that still slip through.
DELAY = float(os.environ.get("DELAY", "4.0"))  # seconds between every fetch
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
# TopCV quotes salaries in VND; the contract's salary fields are USD, so the
# parser converts at this rate. Contract-fed so it can be refreshed without a
# code change.
VND_PER_USD = float(os.environ.get("VND_PER_USD", "25000"))

# ── from the contract's `kafka:` block ───────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.topcv")
SCHEMA_SUBJECT = f"{KAFKA_TOPIC}-value"

# The .avsc ships at a fixed path inside the image, so this is not contract-fed.
SCHEMA_PATH = Path(
    os.environ.get(
        "SCHEMA_PATH",
        _REPO_ROOT / "data-contracts" / "schemas" / "raw" / "topcv.avsc",
    )
)
