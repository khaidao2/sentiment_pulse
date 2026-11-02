"""Verified prerequisites between canonical skills, as a PARTIAL order.

An edge means "B cannot be learned without A". It orders a learner's months, so
the bar is definitional dependence -- superset, subfield, or operates-on -- and
never statistical association. The co-occurrence study that settled this is
summarised at the top of prerequisites.yaml; the short version is that
directional asymmetry in job postings is 100% determined by skill popularity
(3,767 pairs agree, 0 disagree) and says "learn CI/CD before Git".

PARTIAL, NOT TOTAL. Most skills have no prerequisite and no position. `layers()`
puts every skill with no unmet dependency into the SAME layer, so "we do not
know of an order between these" renders as one unordered group rather than an
arbitrary sequence. A complete DAG over 113 skills would be a fabrication.

TWO KINDS OF EVIDENCE, AND NEITHER IS A TIER. `curated` is a human's claim with
a stated reason; `provider_order` would be a sequence a provider itself
published. They are PREREQUISITE evidence. The `role`/`qualifier` tiers in
roles/ are sequencing metadata for job titles and have nothing to do with this
file -- conflating them would let a title-matching heuristic become a claim
about what must be learned first.

No `provider_order` edges ship yet. Microsoft orders MODULES inside a learning
path, and a module sequence is not a skill dependence: two consecutive modules
may teach unrelated skills. Deriving edges from it would manufacture exactly the
unfounded ordering this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from skills.dictionary import ALL_SKILLS

PATH = Path(__file__).parent / "prerequisites.yaml"

CURATED = "curated"
PROVIDER_ORDER = "provider_order"
EVIDENCE_KINDS = (CURATED, PROVIDER_ORDER)


class PrerequisiteError(RuntimeError):
    """The table is malformed, names an unknown skill, or contains a cycle."""


@dataclass(frozen=True)
class Prerequisite:
    skill: str           # the skill that has the dependency
    requires: str        # the skill that must come first
    why: str             # the stated reason; an edge without one is rejected
    evidence_kind: str = CURATED
    curated_by: str = ""  # who is accountable for the claim


def _detect_cycle(edges: list[Prerequisite]) -> list[str] | None:
    """Return one cycle as a path, or None. Iterative DFS with colouring.

    A cycle is unsatisfiable, not merely odd: it says each of two skills must be
    learned before the other, so any path built from it would be arbitrary.
    Rejected at load, where a human is looking, rather than at use.
    """
    graph: dict[str, list[str]] = {}
    for e in edges:
        graph.setdefault(e.requires, []).append(e.skill)
        graph.setdefault(e.skill, [])

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)

    for root in sorted(graph):
        if colour[root] != WHITE:
            continue
        stack = [(root, iter(sorted(graph[root])))]
        path = [root]
        colour[root] = GREY
        while stack:
            node, children = stack[-1]
            nxt = next(children, None)
            if nxt is None:
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if colour.get(nxt) == GREY:
                return path[path.index(nxt):] + [nxt]
            if colour.get(nxt) == WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, iter(sorted(graph[nxt]))))
    return None


def _load(path: Path | None = None) -> list[Prerequisite]:
    p = Path(path or PATH)
    if not p.exists():
        raise PrerequisiteError(f"{p} is missing")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise PrerequisiteError(f"{p}: top level must be a mapping")
    unknown = set(doc) - {"prerequisites", "curated_by"}
    if unknown:
        raise PrerequisiteError(f"{p}: unknown section(s) {sorted(unknown)}")

    default_by = (doc.get("curated_by") or "").strip()
    if not default_by:
        raise PrerequisiteError(
            f"{p}: `curated_by` is required -- every edge is a claim someone "
            f"has to be willing to defend."
        )

    edges: list[Prerequisite] = []
    seen: set[tuple[str, str]] = set()
    for i, raw in enumerate(doc.get("prerequisites") or []):
        if not isinstance(raw, dict):
            raise PrerequisiteError(f"{p}: entry {i} must be a mapping")
        skill, requires = raw.get("skill"), raw.get("requires")
        why = (raw.get("why") or "").strip()
        kind = raw.get("evidence_kind", CURATED)

        for name in (skill, requires):
            # The same rule overrides.yaml and tag_map.yaml follow: this file
            # relates skills, it does not get to invent them.
            if name not in ALL_SKILLS:
                raise PrerequisiteError(
                    f"{p}: entry {i} names {name!r}, which is not in "
                    f"skills.dictionary. Add it there first."
                )
        if skill == requires:
            raise PrerequisiteError(f"{p}: entry {i}: {skill!r} requires itself")
        if kind not in EVIDENCE_KINDS:
            raise PrerequisiteError(
                f"{p}: entry {i} has evidence_kind {kind!r}; allowed: "
                f"{list(EVIDENCE_KINDS)}. Statistical association is not evidence."
            )
        if not why:
            # An edge nobody can justify in a sentence is an edge nobody
            # reviewed. The reason is stored, so it has to exist.
            raise PrerequisiteError(f"{p}: entry {i} ({skill} <- {requires}) has no `why`")
        if (skill, requires) in seen:
            raise PrerequisiteError(f"{p}: duplicate edge {skill} <- {requires}")
        seen.add((skill, requires))
        edges.append(Prerequisite(skill, requires, why, kind,
                                  (raw.get("by") or default_by).strip()))

    cycle = _detect_cycle(edges)
    if cycle:
        raise PrerequisiteError(f"{p}: prerequisite cycle: {' -> '.join(cycle)}")
    return edges


_EDGES: list[Prerequisite] | None = None


def all_edges() -> list[Prerequisite]:
    global _EDGES
    if _EDGES is None:
        _EDGES = _load()
    return _EDGES


def required_by(skill: str) -> list[str]:
    """Direct prerequisites of one skill, in a stable order. Usually empty."""
    return sorted(e.requires for e in all_edges() if e.skill == skill)


def layers(skills) -> list[list[str]]:
    """Group `skills` into ordered layers; within a layer there is NO order.

    Only edges BETWEEN the given skills constrain anything -- a prerequisite the
    learner already has, or that is not on this path, must not hold a skill back.

    The return shape is the honest one: a list of unordered groups. A caller
    that wants a numbered sequence has to decide for itself how to break ties
    this module refuses to break, and should not.
    """
    want = list(dict.fromkeys(skills))
    inside = set(want)
    deps = {s: {e.requires for e in all_edges()
                if e.skill == s and e.requires in inside} for s in want}

    out: list[list[str]] = []
    placed: set[str] = set()
    while len(placed) < len(want):
        ready = sorted(s for s in want if s not in placed and deps[s] <= placed)
        if not ready:
            # Unreachable: _load rejects cycles. Loud rather than an infinite
            # loop if that ever stops being true.
            raise PrerequisiteError(
                f"cannot order {sorted(set(want) - placed)}: cycle among the inputs"
            )
        out.append(ready)
        placed.update(ready)
    return out
