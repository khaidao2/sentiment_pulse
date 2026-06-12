"""vnexpress RSS and HTML crawler.

Fetches articles from configured VnExpress RSS feeds, downloads full article
bodies using `curl-cffi`, parses metadata and body using `BeautifulSoup`,
maps to the Avro news schema, and publishes them to Kafka.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Protocol

from curl_cffi import requests
from bs4 import BeautifulSoup

try:  # works both as a package (-m) and as flat scripts
    from .config import CrawlerConfig
    from .mapper import to_record
except ImportError:  # pragma: no cover
    from config import CrawlerConfig
    from mapper import to_record

logger = logging.getLogger("news_crawler")


class Producer(Protocol):
    """Minimal contract the crawler needs from a producer."""

    def send(self, record: dict) -> Any: ...


class NewsCrawler:
    def __init__(
        self,
        producer: Producer,
        config: Optional[CrawlerConfig] = None,
    ):
        self.producer = producer
        self.config = config or CrawlerConfig()

    def fetch_rss_items(self, feed_name: str, feed_url: str) -> List[Dict[str, Any]]:
        """Fetch RSS feed items and return a list of dictionaries with basic metadata."""
        logger.info("Fetching RSS feed %s from %s", feed_name, feed_url)
        try:
            response = requests.get(feed_url, impersonate="chrome110", timeout=15)
            if response.status_code != 200:
                logger.error("Failed to fetch RSS feed %s: HTTP %d", feed_name, response.status_code)
                return []

            root = ET.fromstring(response.content)
            items = []
            for item in root.findall(".//item"):
                title_elem = item.find("title")
                link_elem = item.find("link")
                pub_date_elem = item.find("pubDate")

                title = title_elem.text if title_elem is not None else ""
                link = link_elem.text if link_elem is not None else ""
                pub_date_raw = pub_date_elem.text if pub_date_elem is not None else ""

                # Standardize publish time to ISO format
                published_at = pub_date_raw
                if pub_date_raw:
                    try:
                        published_at = parsedate_to_datetime(pub_date_raw).isoformat()
                    except Exception:
                        logger.warning("Could not parse pubDate string: %s", pub_date_raw)

                items.append({
                    "title": title,
                    "url": link,
                    "published_at": published_at
                })

            logger.info("Found %d items in feed %s", len(items), feed_name)
            return items
        except Exception as e:
            logger.error("Error fetching or parsing RSS feed %s: %s", feed_name, e)
            return []

    def scrape_article(self, url: str) -> Dict[str, Any]:
        """Scrape the full HTML body and metadata of an article using curl-cffi and BeautifulSoup."""
        logger.info("Scraping full article HTML from %s", url)
        try:
            response = requests.get(url, impersonate="chrome110", timeout=15)
            if response.status_code != 200:
                logger.error("Failed to fetch article %s: HTTP %d", url, response.status_code)
                return {}

            soup = BeautifulSoup(response.content, "lxml")

            # Extract main content container
            content_container = soup.find("article", class_="fck_detail") or soup.find("div", class_="fck_detail")
            
            paragraphs = []
            if content_container:
                for p in content_container.find_all("p", class_="Normal"):
                    # Check if paragraph has text
                    p_text = p.get_text(strip=True)
                    if p_text:
                        paragraphs.append(p_text)

            content_text = "\n".join(paragraphs) if paragraphs else ""

            # Extract author
            # Typically, the author is the last paragraph in the fck_detail body, or has a specific alignment style.
            author = None
            if paragraphs:
                last_p = content_container.find_all("p", class_="Normal")[-1] if content_container else None
                if last_p and last_p.get("style") and "text-align:right" in last_p.get("style").replace(" ", ""):
                    author = last_p.get_text(strip=True)
                else:
                    # Fallback to the last paragraph text
                    author = paragraphs[-1]
            
            return {
                "content": content_text,
                "author": author
            }
        except Exception as e:
            logger.error("Error scraping article %s: %s", url, e)
            return {}

    def crawl_feed(self, name: str, url: str) -> int:
        """Fetch and scrape all items for a single RSS feed."""
        items = self.fetch_rss_items(name, url)
        
        # Limit to configured count
        items_to_crawl = items[:self.config.max_articles_per_feed]
        logger.info("Crawl list filtered to %d items for feed %s", len(items_to_crawl), name)

        sent = 0
        for item in items_to_crawl:
            article_url = item["url"]
            # Filter out non-article pages (video pages, podcasts, foreign language feeds, etc.)
            if not article_url or any(sub in article_url for sub in ["video.", "podcast.", "e."]):
                logger.info("Skipping unsupported/media link: %s", article_url)
                continue

            # Scraping full article
            details = self.scrape_article(article_url)
            if not details or not details.get("content"):
                logger.warning("No content extracted for %s, skipping", article_url)
                continue

            item.update(details)

            # Map to schema dict
            record = to_record(name, item)

            # Produce to Kafka
            try:
                self.producer.send(record)
                sent += 1
            except Exception as e:
                logger.error("Failed to produce record to Kafka: %s", e)

            # Rate limit politeness delay
            time.sleep(self.config.request_delay)

        logger.info("Feed %s completed: %d/ %d records successfully sent to Kafka", name, sent, len(items_to_crawl))
        return sent

    def crawl(self) -> int:
        """Main orchestrator to run news crawler over all configured feeds."""
        logger.info("Starting news crawl for %d feeds", len(self.config.feeds))
        total_records = 0
        for name, url in self.config.feeds.items():
            try:
                total_records += self.crawl_feed(name, url)
            except Exception as e:
                logger.error("Unexpected error crawling feed %s: %s", name, e)
        logger.info("News crawl finished. Total produced: %d records", total_records)
        return total_records
