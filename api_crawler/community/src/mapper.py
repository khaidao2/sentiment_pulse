"""Map raw crawled community items to ``CommunityRecord`` schema dictionary format."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping


def to_record(item: Mapping[str, Any]) -> dict:
    """Build one Avro-conformant record from a single crawled community post.

    Target schema (api_crawler/community/producer.py / sink.py):
        id, title, content, author, source, url, published_at, created_at
    """
    post_id = item.get("postId", "")
    author = item.get("user_FullName")
    if author:
        author = str(author).strip()
        # Clean up any trailing/leading parentheses or email address if needed
        author = re.sub(r'^\(?(.*?)\)?$', r'\1', author).strip()
        
    if not author:
        author = None

    content = str(item.get("textContent", "")).strip()

    # Generate a descriptive title from content
    # Use first line or first 100 characters
    title = ""
    if content:
        first_line = content.split("\n")[0].strip()
        if len(first_line) > 100:
            title = first_line[:97] + "..."
        else:
            title = first_line
    if not title:
        title = f"Post by {author or 'anonymous'}"

    # Reconstruct article/post detail page URL
    url = f"https://chungsy.vn/posts/{post_id}" if post_id else None

    # publishDate is already ISO-8601 string from Next.js API, e.g. "2026-06-11T16:09:34.025Z"
    published_at = item.get("publishDate", "")

    return {
        "id": f"chungsy:{post_id}" if post_id else f"chungsy_hash:{hash(content)}",
        "title": title,
        "content": content,
        "author": author,
        "source": "chungsy",
        "url": url,
        "published_at": str(published_at).strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
