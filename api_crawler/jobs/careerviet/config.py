"""Per-source config for the careerviet crawler.

Values come from the environment, which in production is injected by the
sent-gen-generated Airflow DAG from the contract YAML
(data-contracts/contracts/producer/careerviet.yaml) — that YAML is the single
source of truth. The literals below are only dev fallbacks for running locally
without the DAG.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── from the contract's `crawler:` block (DAG injects as UPPER_CASE env) ──
# The IT / software category listing ("CNTT - Phần mềm", c1). Paging appends
# `-trang-N` before the locale suffix; CareerVietSource builds that.
LISTING_URL = os.environ.get(
    "LISTING_URL", "https://careerviet.vn/viec-lam/cntt-phan-mem-c1-vi.html"
)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
DELAY = float(os.environ.get("DELAY", "0.5"))  # seconds between detail fetches

# CareerViet quotes salaries in VND. Every source normalises to USD so the
# salary_*_usd columns share one unit across sources (silver_history unions them
# with `select *`). Contract-fed: crawler.vnd_per_usd.
VND_PER_USD = float(os.environ.get("VND_PER_USD", "25000"))

# ── from the contract's `kafka:` block ───────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.careerviet")
SCHEMA_SUBJECT = f"{KAFKA_TOPIC}-value"

# The .avsc ships at a fixed path inside the image, so this is not contract-fed.
SCHEMA_PATH = Path(
    os.environ.get(
        "SCHEMA_PATH",
        _REPO_ROOT / "data-contracts" / "schemas" / "raw" / "careerviet.avsc",
    )
)
