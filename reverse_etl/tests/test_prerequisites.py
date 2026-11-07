"""A prerequisite edge orders a learner's months, so the bar is high.

The hazards guarded here:

  * an edge the dictionary has never heard of, which would relate a skill that
    does not exist;
  * a cycle, which says each of two skills must precede the other and makes any
    path built from it arbitrary;
  * co-occurrence creeping in as evidence -- the one thing the measurement
    showed cannot order skills;
  * a total order being invented where the data supports a partial one. Skills
    with no known relationship must come back as ONE unordered group, not a
    sequence;
  * an edge with no stated reason, which is an edge nobody reviewed.
"""

import textwrap

import pytest

import skills.prerequisites as pr
from skills.dictionary import ALL_SKILLS
from skills.prerequisites import (
    CURATED,
    PROVIDER_ORDER,
    PrerequisiteError,
    all_edges,
    layers,
    required_by,
)


@pytest.fixture(autouse=True)
def _reset():
    pr._EDGES = None
    yield
    pr._EDGES = None


def _table(tmp_path, body: str):
    p = tmp_path / "prerequisites.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ── the shipped table ────────────────────────────────────────────────────────


def test_shipped_table_is_valid_and_every_skill_is_canonical():
    edges = all_edges()
    assert edges
    for e in edges:
        assert e.skill in ALL_SKILLS and e.requires in ALL_SKILLS
        assert e.why.strip(), f"{e.skill} <- {e.requires} has no reason"
        assert e.curated_by, "every claim needs someone accountable"
        assert e.evidence_kind in (CURATED, PROVIDER_ORDER)


def test_shipped_table_is_acyclic():
    assert pr._detect_cycle(all_edges()) is None


def test_the_graph_stays_partial():
    """Far fewer edges than skills: most skills have no known prerequisite."""
    edges = all_edges()
    assert len(edges) < len(ALL_SKILLS) / 2
    assert required_by("SQL") == [], "a root skill must have no prerequisite"
    assert required_by("Kubernetes") == ["Docker"]


# ── it cannot invent skills, reasons, or evidence ────────────────────────────


def test_an_unknown_skill_is_a_hard_error(tmp_path):
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Kubernetes
            requires: Totally Made Up Thing
            why: because
    """)
    with pytest.raises(PrerequisiteError, match="not in skills.dictionary"):
        pr._load(p)


def test_an_edge_without_a_reason_is_rejected(tmp_path):
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Kubernetes
            requires: Docker
            why: ""
    """)
    with pytest.raises(PrerequisiteError, match="has no `why`"):
        pr._load(p)


def test_a_table_without_a_curator_is_rejected(tmp_path):
    p = _table(tmp_path, """
        prerequisites:
          - skill: Kubernetes
            requires: Docker
            why: orchestrates containers
    """)
    with pytest.raises(PrerequisiteError, match="curated_by"):
        pr._load(p)


def test_co_occurrence_is_not_an_allowed_evidence_kind(tmp_path):
    """The measured finding, encoded as a rule rather than a convention."""
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Kubernetes
            requires: Docker
            why: lift 3.88 and positive asymmetry
            evidence_kind: co_occurrence
    """)
    with pytest.raises(PrerequisiteError, match="evidence_kind"):
        pr._load(p)


def test_a_self_edge_is_rejected(tmp_path):
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Docker
            requires: Docker
            why: nonsense
    """)
    with pytest.raises(PrerequisiteError, match="requires itself"):
        pr._load(p)


def test_a_duplicate_edge_is_rejected(tmp_path):
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Kubernetes
            requires: Docker
            why: one
          - skill: Kubernetes
            requires: Docker
            why: two
    """)
    with pytest.raises(PrerequisiteError, match="duplicate edge"):
        pr._load(p)


def test_a_missing_file_is_fatal(tmp_path):
    with pytest.raises(PrerequisiteError, match="missing"):
        pr._load(tmp_path / "nope.yaml")


# ── cycles ───────────────────────────────────────────────────────────────────


def test_a_two_node_cycle_is_rejected_at_load(tmp_path):
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Docker
            requires: Kubernetes
            why: a
          - skill: Kubernetes
            requires: Docker
            why: b
    """)
    with pytest.raises(PrerequisiteError, match="cycle"):
        pr._load(p)


def test_a_longer_cycle_is_rejected(tmp_path):
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Docker
            requires: Git
            why: a
          - skill: Git
            requires: Linux
            why: b
          - skill: Linux
            requires: Docker
            why: c
    """)
    with pytest.raises(PrerequisiteError, match="cycle"):
        pr._load(p)


def test_a_diamond_is_not_a_cycle(tmp_path):
    """Two independent paths to one skill is legal and must still load."""
    p = _table(tmp_path, """
        curated_by: tester
        prerequisites:
          - skill: Kubernetes
            requires: Docker
            why: a
          - skill: Kubernetes
            requires: Linux
            why: b
          - skill: Docker
            requires: Git
            why: c
          - skill: Linux
            requires: Git
            why: d
    """)
    assert len(pr._load(p)) == 4


# ── partial ordering: unknown order renders as unordered ─────────────────────


def test_skills_with_no_known_order_land_in_one_layer():
    got = layers(["SQL", "Python", "Linux"])
    assert got == [["Linux", "Python", "SQL"]], \
        "no known relationship must be ONE unordered group, not a sequence"


def test_a_prerequisite_pushes_a_skill_to_a_later_layer():
    got = layers(["Kubernetes", "Docker"])
    assert got == [["Docker"], ["Kubernetes"]]


def test_an_out_of_scope_prerequisite_does_not_hold_a_skill_back():
    """Only edges BETWEEN the requested skills constrain the ordering."""
    assert layers(["Kubernetes"]) == [["Kubernetes"]]


def test_layers_cover_every_input_exactly_once():
    want = ["Kubernetes", "Docker", "CI/CD", "Git", "Linux", "SQL"]
    got = layers(want)
    flat = [s for layer in got for s in layer]
    assert sorted(flat) == sorted(want)
    assert len(flat) == len(set(flat))


def test_layers_is_deterministic():
    want = ["Kubernetes", "Docker", "CI/CD", "Git", "Linux"]
    assert layers(want) == layers(want)


def test_a_prerequisite_never_appears_after_its_dependant():
    got = layers([e.skill for e in all_edges()] + [e.requires for e in all_edges()])
    pos = {s: i for i, layer in enumerate(got) for s in layer}
    for e in all_edges():
        assert pos[e.requires] < pos[e.skill], f"{e.requires} must precede {e.skill}"
