"""Nightly: infer job -> skill edges a dictionary scan cannot see.

    python match_jobs_ai.py            # dry run: the plan and a sample
    python match_jobs_ai.py --apply    # write

The exact-match extractor finds a skill when a posting names it. It cannot
find CI/CD in "GitLab runners build and deploy on every merge", and that
phrasing is common. This asks a model, and then refuses to let the answer near
anything the product calls demand.

WHAT MAKES THIS SAFE, in the order the guarantees matter:

  1. EVERY EDGE IS method='model'. Official demand -- a family's sample size,
     the 10% floor, a skill's rank, a learner's readiness -- is computed from
     method='dictionary' only, enforced in pg_learning_repository and pinned by
     integration tests that inject model edges and assert demand is
     byte-identical. Nothing written here can move a published number.
  2. A DICTIONARY EDGE IS NEVER TOUCHED. Skills a job already has from the
     extractor are removed from the model's answer before writing, and the
     primary key would refuse the write anyway. No UPDATE, no DELETE against
     a dictionary row appears in this file.
  3. THE VOCABULARY DECIDES, as everywhere else. The model is handed the
     canonical list and its reply is filtered against ALL_SKILLS here. This
     module does not need the OPA sandbox the web backend uses: the backend
     has no dictionary and must ask a policy what is allowed, while the
     pipeline OWNS the dictionary and checks against the thing itself.
  4. CONFIDENCE AND A REASON ARE REQUIRED. An inference nobody can read is an
     inference nobody can review, and the schema refuses an edge without both.

IDEMPOTENT AND BOUNDED. job_model_match records which postings have been seen,
so a re-run processes nothing twice and a night's work has a fixed budget. A
job where the model found nothing is recorded with edges=0 -- otherwise it
would be re-asked for ever, paying repeatedly for the same empty answer while
the rest of the corpus waited.

FAILURE IS PER-JOB. One posting's gateway error or unparseable reply is
counted and skipped; the run continues. The task fails only if the failure
RATE crosses a threshold, because a handful of bad replies is weather and half
the batch failing is an outage.

Env:
  POSTGRES_DSN
  AI_GATEWAY_ENDPOINT / AI_GATEWAY_API_KEY / AI_GATEWAY_MODEL
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request

import psycopg

from skills.dictionary import ALL_SKILLS
from skills.extractor import normalize

#: One night's budget. 1,493 postings at one call each is neither fast nor
#: cheap, and a nightly job that might run for an hour is a nightly job that
#: will one day overlap itself. The corpus is covered over several nights and
#: the watermark makes that safe.
MAX_JOBS_PER_RUN = 200

#: Above this share of per-job failures the run is an outage rather than
#: weather, and the task should go red instead of quietly covering 3 postings.
MAX_FAILURE_RATE = 0.25

TIMEOUT_SECONDS = 60

_SYSTEM = (
    "You label a job posting with the technical skills it requires.\n"
    "You are given an ALLOWED list of skill names. Return ONLY names copied "
    "exactly from that list.\n"
    "Return a JSON array of objects: "
    '[{"skill": "<name from the list>", "confidence": <0.0-1.0>, '
    '"why": "<the words in the posting that show it>"}]\n'
    "Include a skill when the posting gives evidence of it even if it does not "
    "use the same word: 'GitLab runners build and deploy on every merge' is "
    "evidence of CI/CD; 'orchestrate containers with Helm' is evidence of "
    "Kubernetes.\n"
    "Set confidence below 0.5 when the evidence is indirect.\n"
    "`why` must quote or closely paraphrase the posting. Do not invent "
    "evidence. If the posting names no skill from the list, return []."
)


def _ask(endpoint: str, key: str, model: str, text: str, allowed: list[str]) -> list[dict]:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "ALLOWED:\n" + "\n".join(allowed)
                                        + "\n\nPOSTING:\n" + text},
        ],
    }).encode()
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        content = json.load(r)["choices"][0]["message"]["content"]
    return _parse(content)


def _parse(content: str) -> list[dict]:
    """Recover the array; refuse to guess at anything else.

    Models fence JSON and prepend commentary however firmly told not to, so
    recovering the array is worth doing. A shape we did not expect means the
    prompt contract broke, and proceeding with fragments would write edges
    nobody can account for.
    """
    text = (content or "").strip()
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON array in the reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("reply was not a JSON array")
    return [d for d in data if isinstance(d, dict)]


def validated(raw: list[dict], allowed_lower: dict[str, str],
              already: set[str]) -> list[tuple[str, float, str]]:
    """(canonical, confidence, why) for the entries the vocabulary accepts.

    Drops, in this order: a name the dictionary does not have, a skill the
    posting already has from the extractor (point 2 -- dictionary evidence is
    never replaced), an unusable confidence, and a missing reason. Each is a
    silent-corruption risk if waved through, and none is worth a partial row.
    """
    out: list[tuple[str, float, str]] = []
    seen: set[str] = set()
    for item in raw:
        name = allowed_lower.get(normalize(item.get("skill")))
        if name is None or name in already or name in seen:
            continue
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= conf <= 1.0:
            continue
        why = str(item.get("why") or "").strip()
        if not why:
            continue
        seen.add(name)
        out.append((name, conf, why[:1000]))
    return out


def _pending(cur, limit: int) -> list[tuple[str, str]]:
    """Postings nobody has asked a model about yet, oldest crawl first.

    Oldest first so coverage advances deterministically instead of
    re-sampling whatever happens to be newest.
    """
    cur.execute(
        """
        SELECT j.job_id,
               concat_ws(E'\\n', j.title, j.description,
                         array_to_string(j.requirements, E'\\n'))
        FROM jobs j
        LEFT JOIN job_model_match m USING (job_id)
        WHERE m.job_id IS NULL
        ORDER BY j.crawled_at NULLS FIRST
        LIMIT %s
        """,
        (limit,),
    )
    return [(r[0], r[1] or "") for r in cur.fetchall()]


def _dictionary_skills(cur, job_id: str) -> set[str]:
    cur.execute(
        """
        SELECT s.name FROM job_skills js JOIN skills s USING (skill_id)
        WHERE js.job_id = %s AND js.method = 'dictionary'
        """,
        (job_id,),
    )
    return {r[0] for r in cur.fetchall()}


def _skill_ids(cur, names: set[str]) -> dict[str, int]:
    """Resolve against the SHARED lookup. Never inserts.

    Unlike sync_jobs, this cannot discover a skill: every name here was
    validated against ALL_SKILLS, so a name with no row means the job sync has
    not run yet. Minting it would create a skill nothing else points at.
    """
    if not names:
        return {}
    cur.execute("SELECT name, skill_id FROM skills WHERE name = ANY(%s)",
                (sorted(names),))
    return dict(cur.fetchall())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--limit", type=int, default=MAX_JOBS_PER_RUN)
    args = ap.parse_args()

    endpoint = os.environ.get("AI_GATEWAY_ENDPOINT", "")
    key = os.environ.get("AI_GATEWAY_API_KEY", "")
    model = os.environ.get("AI_GATEWAY_MODEL", "")
    if not (endpoint and key and model):
        raise SystemExit(
            "AI_GATEWAY_ENDPOINT, AI_GATEWAY_API_KEY and AI_GATEWAY_MODEL must "
            "all be set; the ai-gateway-credentials secret carries them."
        )

    allowed = sorted(ALL_SKILLS)
    allowed_lower = {normalize(a): a for a in allowed}

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    with pg.cursor() as cur:
        for table in ("job_model_match", "job_skills"):
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0] is None:
                raise SystemExit(
                    f"{table} does not exist. The web app owns this schema and "
                    f"has to migrate it (20261103020000_add_skill_provenance.sql). "
                    f"Nothing was written."
                )

        jobs = _pending(cur, args.limit)
        if not jobs:
            print("match_jobs_ai: every posting has been looked at. Nothing to do.")
            return
        print(f"match_jobs_ai: {len(jobs)} posting(s) to look at "
              f"(budget {args.limit}), model={model}")

        written = failures = empty = 0
        samples: list[str] = []
        for job_id, text in jobs:
            if not text.strip():
                # Nothing to read. Recorded so it is not re-asked nightly.
                if args.apply:
                    cur.execute(
                        "INSERT INTO job_model_match (job_id, model, edges) "
                        "VALUES (%s, %s, 0) ON CONFLICT (job_id) DO NOTHING",
                        (job_id, model))
                empty += 1
                continue
            try:
                raw = _ask(endpoint, key, model, text[:12000], allowed)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                # Per-job isolation: one bad posting must not cost the batch.
                failures += 1
                print(f"  ! {job_id[:12]} {type(exc).__name__}")
                continue

            edges = validated(raw, allowed_lower, _dictionary_skills(cur, job_id))
            ids = _skill_ids(cur, {n for n, _, _ in edges})
            if args.apply:
                # Reconcile this job's MODEL edges only. A dictionary row is
                # not in scope of this delete and cannot be.
                cur.execute(
                    "DELETE FROM job_skills WHERE job_id = %s AND method = 'model'",
                    (job_id,))
                for name, conf, why in edges:
                    sid = ids.get(name)
                    if sid is None:
                        continue
                    cur.execute(
                        "INSERT INTO job_skills "
                        "  (job_id, skill_id, method, confidence, rationale) "
                        "VALUES (%s, %s, 'model', %s, %s) "
                        "ON CONFLICT (job_id, skill_id) DO NOTHING",
                        (job_id, sid, conf, why))
                cur.execute(
                    "INSERT INTO job_model_match (job_id, model, edges) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (job_id) DO UPDATE SET "
                    "  matched_at = now(), model = EXCLUDED.model, "
                    "  edges = EXCLUDED.edges",
                    (job_id, model, len(edges)))
            written += len(edges)
            if edges and len(samples) < 5:
                n, c, w = edges[0]
                samples.append(f"  {job_id[:12]}  {n} ({c:.2f}) — {w[:70]}")

        rate = failures / len(jobs)
        print(f"\nmatch_jobs_ai: {written} model edge(s) over {len(jobs)} posting(s); "
              f"{failures} failed, {empty} had no text")
        if samples:
            print("  sample inferences:")
            print("\n".join(samples))
        if not args.apply:
            print("\nmatch_jobs_ai: DRY RUN — nothing written.")
            return
        if rate > MAX_FAILURE_RATE:
            # Commit what succeeded, then fail the task. Throwing away good
            # work would mean re-paying for it, and the watermark makes the
            # retry cheap.
            pg.commit()
            raise SystemExit(
                f"match_jobs_ai: {failures}/{len(jobs)} postings failed "
                f"({rate:.0%}, limit {MAX_FAILURE_RATE:.0%}). Committed the "
                f"{written} edge(s) that succeeded; failing so this is visible."
            )
        pg.commit()
        print("match_jobs_ai: committed. These edges do NOT count toward demand.")


if __name__ == "__main__":
    main()
