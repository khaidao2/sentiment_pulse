"""Make the repo root and the vn_stock crawler package importable in tests."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRAWLER_SRC = os.path.join(ROOT, "api_crawler", "vn_stock", "src")

for path in (ROOT, CRAWLER_SRC):
    if path not in sys.path:
        sys.path.insert(0, path)
