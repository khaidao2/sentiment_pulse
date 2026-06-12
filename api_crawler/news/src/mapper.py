"""Map raw crawled news items to ``NewsRecord`` schema dictionary format."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping


def to_record(feed_name: str, item: Mapping[str, Any]) -> dict:
    """Build one Avro-conformant record from a single crawled news item.

    Target schema (api_crawler/news/producer.py / sink.py):
        id, title, content, author, source, url, published_at, created_at
    """
    url = item.get("url", "")
    
    # Generate unique ID: vnexpress:<numeric_id> or fallback to md5 hash
    # Example: https://vnexpress.net/...-5084704.html -> vnexpress:5084704
    url_id = ""
    if url:
        match = re.search(r'-(\d+)\.html$', url)
        if match:
            url_id = match.group(1)
        else:
            url_id = hashlib.md5(url.encode("utf-8")).hexdigest()
    else:
        # Fallback if URL is missing
        title = item.get("title", "")
        url_id = hashlib.md5(title.encode("utf-8")).hexdigest()

    record_id = f"vnexpress:{url_id}"

    # Handle author value
    author = item.get("author")
    if author:
        author = str(author).strip()
        # Clean up any trailing/leading parentheses or whitespaces
        author = re.sub(r'^\(?(.*?)\)?$', r'\1', author).strip()
    
    # If author is empty string, convert to None (null in schema)
    if not author:
        author = None

    return {
        "id": record_id,
        "title": str(item.get("title", "")).strip(),
        "content": str(item.get("content", "")).strip(),
        "author": author,
        "source": "vnexpress",
        "url": url,
        "published_at": str(item.get("published_at", "")).strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
