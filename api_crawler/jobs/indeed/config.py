"""Per-source config for the Indeed Vietnam crawler.

Values come from the environment, which in production is injected by the
sent-gen-generated Airflow DAG from the contract YAML
(data-contracts/contracts/producer/indeed.yaml) — that YAML is the single
source of truth. The literals below are only dev fallbacks for running locally
without the DAG.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── from the contract's `crawler:` block (DAG injects as UPPER_CASE env) ──
SEARCH_URL = os.environ.get("SEARCH_URL", "https://vn.indeed.com/jobs")

#: Indeed has no IT-only listing page the way itviec does, so the crawl is
#: driven by queries. Comma-separated in the env because that is what a DAG
#: env var can carry.
QUERIES = [q.strip() for q in os.environ.get(
    "QUERIES",
    "data engineer,backend developer,frontend developer,devops engineer,"
    "data analyst,software engineer",
).split(",") if q.strip()]

#: Empty means the whole country, which is what vn.indeed.com already scopes.
LOCATION = os.environ.get("LOCATION", "")

#: ONE page per query. robots.txt allows start=0..90, but Indeed answers an
#: anonymous request for page two with its sign-in page, so depth is not
#: available without crawling as a logged-in user. ~15 cards per query; breadth
#: comes from QUERIES. Kept configurable in case that wall moves.
MAX_PAGES = int(os.environ.get("MAX_PAGES", "1"))

#: Seconds between search pages. An order of magnitude above the other sources:
#: Indeed starts answering 403 after roughly half a dozen pages from one
#: address, and at one page per query the whole crawl is still a few minutes.
DELAY = float(os.environ.get("DELAY", "15.0"))

#: First wait after a refusal; the second attempt waits twice this. Three
#: attempts per page, so a transient block costs three minutes, not a query.
BACKOFF = float(os.environ.get("BACKOFF", "60.0"))
ATTEMPTS = int(os.environ.get("ATTEMPTS", "3"))

# ── from the contract's `kafka:` block ───────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.indeed")
SCHEMA_SUBJECT = f"{KAFKA_TOPIC}-value"

# The .avsc ships at a fixed path inside the image, so this is not contract-fed.
SCHEMA_PATH = Path(
    os.environ.get(
        "SCHEMA_PATH",
        _REPO_ROOT / "data-contracts" / "schemas" / "raw" / "indeed.avsc",
    )
)
