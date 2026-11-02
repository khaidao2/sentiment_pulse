"""Match the landed course catalogue to canonical skills. Report, or hand on.

    python -m courses.match              # read-only: the coverage report
    python -m courses.match --sample Azure   # ...plus 10 titles behind one skill

Reads `silver_courses` in ClickHouse — one row per course id, deduped from the
Parquet that api_crawler.sink landed in MinIO — and never the web Postgres. Not
`bronze_courses`: the crawl re-reads every catalogue weekly, so bronze holds one
copy of a course per run and matching it there would multiply every row.

This module WRITES NOTHING. It is the matching rule and its report; the landing
is sync_course_skills.py, which imports `build_edges` from here and UPSERTs the
result into the web Postgres through the same reverse-ETL path sync_jobs.py and
sync_courses.py use. That split is deliberate — see the history below.

HISTORY, because the docstring that used to be here was wrong in a way worth
recording. This module once claimed to write `catalog_candidates.yaml`, which a
curator would promote into `catalog.yaml`, "the only file that ever gets
synced". Neither file ever existed, nothing read them, and the generated header
even named `courses.crawl`, a module that does not exist. A hand-curated YAML
catalogue was also a second source of truth beside the warehouse: this repo
lands data crawler -> Kafka -> MinIO -> dbt -> reverse-ETL -> Postgres, and a
file promoted by hand is not that path. So the YAML emitter is gone. What
survives of the curation idea is overrides.yaml, which corrects individual
verdicts and cannot invent a skill (see courses/overrides.py).

Fetching lives in the crawler, matching lives here, and that split is the point.
The canonical dictionary is in reverse_etl/skills, tuned against this market's
postings; a crawler that guessed at skills would be a second place deciding what
a skill is. So the crawler stores facts and a link, and this reads them back.

Two match strengths, by where the skill landed:

  teaches  — the skill is in the TITLE. "Introduction to Kubernetes" teaches
             Kubernetes. This is what a learning-path step links to.
  mentions — only in the summary or the provider's tags. Mentioning Docker is
             not teaching Docker, so these are kept but never offered as the
             course for a step.

A course with no title match is DROPPED, not stored with a guess. The drop count
is reported, because a sudden jump in it means the dictionary and the catalogue
have drifted apart. Measured on the 2026-11-02 crawl: 7,205 courses in silver,
1,244 kept, 5,961 dropped, 64 of 113 canonical skills covered.

THINGS THIS DELIBERATELY DOES NOT DO:

  * No hierarchy inference. `content_kind` tells a LEARNING_PATH from a MODULE
    and the two are not interchangeable, but a parent does NOT inherit its
    children's skills and a child does not inherit its parent's. A learning path
    whose title names no skill is dropped exactly like any other course; the
    alternative is a path "teaching" everything underneath it.
  * No `summary` downstream. It is read here as text to scan and is the one
    reason the column is carried into the raw zone at all. Nothing this module
    produces contains it — only skill names.
  * No level, duration or language. Those are sync_courses.py's business and are
    copied as the source published them; nothing is inferred from a title here.

Env: CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import clickhouse_connect

from skills import extract_skills

from . import overrides as overrides_mod
from . import tag_map

# The crawler's own columns; see data-contracts/schemas/learning/courses.avsc.
# `summary` is read to scan and never written out. The catalogue's facts (level,
# duration, licence, hierarchy) are NOT selected: this module has no use for
# them and sync_courses.py already carries them, so asking for them here would
# only invite someone to derive something from them.
COLUMNS = ["id", "source", "title", "url", "summary", "tags", "content_kind"]

TEACHES = "teaches"
MENTIONS = "mentions"


def client():
    """The ClickHouse client, configured the way every job in this image is."""
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        username=os.environ.get("CLICKHOUSE_USER", "clickhouse"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "default"),
    )


def fetch(ch) -> list[dict]:
    res = ch.query(f"SELECT {', '.join(COLUMNS)} FROM silver_courses")
    return [dict(zip(res.column_names, row)) for row in res.result_rows]


def match_row(row: dict) -> tuple[list[str], list[str]]:
    """The dictionary's raw verdict for one course, before any curation.

    `teaches` scans the title alone. `mentions` scans title + summary + the
    provider's tags and then subtracts `teaches`, so the two lists are disjoint
    by construction and a skill is never stored under both relations.
    """
    tags = list(row.get("tags") or [])
    teaches = extract_skills(description=row["title"])
    everything = extract_skills(
        description=f"{row['title']} {row.get('summary') or ''}",
        tech_stack=tags,
    )
    # The extractor's tag path is exact-match-only, so multi-word labels like
    # "azure virtual machines" matched nothing -- 83% of all course-tag pairs
    # resolved to nothing. tag_map maps the frequent ones and, as importantly,
    # declares which tags are job roles or academic subjects so they stay out.
    # Mapped tags only ever reach `mentions`: a tag is weaker evidence than a
    # title, since a course tagged `azure` may be about something else that
    # merely runs on Azure.
    for name in tag_map.skills_for(tags):
        if name not in everything:
            everything.append(name)
    return teaches, [s for s in everything if s not in teaches]


def match_course(row: dict, ov=None) -> tuple[list[str], list[str]]:
    """The verdict a consumer should use: raw match, then the curation gate."""
    teaches, mentions = match_row(row)
    if ov is None:
        return teaches, mentions
    return ov.apply(row["url"], teaches, mentions)


class Report:
    """What the run saw. Counts only — never a course's prose."""

    def __init__(self):
        self.total = 0
        self.kept = 0
        self.dropped = 0
        self.per_source: Counter[str] = Counter()
        self.per_kind: Counter[tuple[str, str]] = Counter()
        self.per_skill: Counter[str] = Counter()
        self.dropped_with_mentions = 0
        self.stale_override_urls: list[str] = []


def build_edges(rows: list[dict], ov=None) -> tuple[list[tuple[str, str, str]], Report]:
    """(course_id, skill, relation) for every kept course, plus the report.

    A course with no `teaches` after curation contributes NO edges at all, not
    even its `mentions`: "this course mentions Docker" is not a reason to put it
    in a catalogue of what to learn, and storing it would make the mentions-only
    tail look like coverage. 991 of today's 5,961 drops are in exactly that
    state, and they stay out.
    """
    edges: list[tuple[str, str, str]] = []
    report = Report()
    seen_urls: set[str] = set()

    for row in rows:
        report.total += 1
        seen_urls.add(row["url"])
        teaches, mentions = match_course(row, ov)
        if not teaches:
            report.dropped += 1
            if mentions:
                report.dropped_with_mentions += 1
            continue
        report.kept += 1
        report.per_source[row["source"]] += 1
        report.per_kind[(row["source"], row["content_kind"])] += 1
        for skill in teaches:
            report.per_skill[skill] += 1
            edges.append((row["id"], skill, TEACHES))
        for skill in mentions:
            edges.append((row["id"], skill, MENTIONS))

    if ov is not None:
        # A curated url that the catalogue no longer publishes is dead weight,
        # and silently keeping it is how a curation file rots. Named, not
        # removed: the course may simply be missing from this week's crawl.
        report.stale_override_urls = sorted(ov.urls - seen_urls)
    return edges, report


def print_report(report: Report, edges: list[tuple[str, str, str]]) -> None:
    from skills.dictionary import ALL_SKILLS

    teaches_edges = sum(1 for _, _, rel in edges if rel == TEACHES)
    print(f"courses in silver      : {report.total}")
    print(f"no skill in title      : {report.dropped}  (dropped, never stored with a guess)")
    print(f"  of those, mentions-only: {report.dropped_with_mentions}  (still dropped)")
    print(f"kept                   : {report.kept}")
    print(f"edges                  : {len(edges)}  ({teaches_edges} teaches, "
          f"{len(edges) - teaches_edges} mentions)")
    print("\nkept by source and content_kind (a LEARNING_PATH is not a MODULE):")
    for (source, kind), n in sorted(report.per_kind.items()):
        print(f"  {source:<14} {kind:<14} {n:>5}")
    covered = set(report.per_skill)
    print(f"\nskill coverage         : {len(covered)} of {len(ALL_SKILLS)} canonical skills")
    print("  top 15 by course count:")
    for skill, n in report.per_skill.most_common(15):
        print(f"    {skill:<22} {n:>4}")
    missing = sorted(set(ALL_SKILLS) - covered)
    print(f"\n  {len(missing)} skill(s) with NO free course found — the hand-curation list:")
    for i in range(0, len(missing), 6):
        print("    " + ", ".join(missing[i:i + 6]))
    if report.stale_override_urls:
        print(f"\n  {len(report.stale_override_urls)} overrides.yaml url(s) not in this crawl:")
        for url in report.stale_override_urls:
            print(f"    {url}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", metavar="SKILL",
                    help="print up to 10 titles matched to SKILL, to check it by eye")
    args = ap.parse_args()

    ov = overrides_mod.load()
    rows = fetch(client())
    if not rows:
        print("silver_courses is empty — has crawl_courses run, and dbt after it?",
              file=sys.stderr)
        return

    edges, report = build_edges(rows, ov)
    print_report(report, edges)

    if args.sample:
        print(f"\ntitles whose TITLE matched {args.sample!r}:")
        shown = 0
        for row in rows:
            teaches, _ = match_course(row, ov)
            if args.sample in teaches:
                print(f"  {row['source']:<13} {row['content_kind']:<14} {row['title']}")
                shown += 1
                if shown == 10:
                    break
        if not shown:
            print("  (none)")

    print("\nREAD-ONLY. Nothing written: this module reports. "
          "`python sync_course_skills.py --apply` is what lands the edges.")


if __name__ == "__main__":
    main()
