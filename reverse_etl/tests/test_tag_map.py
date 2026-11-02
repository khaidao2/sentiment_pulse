"""The tag table classifies, and cannot quietly become a second dictionary.

Course `tags` are the provider's own labels, not skills. Before this table,
497 of 521 distinct tags -- 83% of all course-tag pairs on the 2026-11-02 crawl
-- resolved to nothing and a free-text scan rescued none of the top 20. The
table exists to give the frequent ones a declared kind so the remainder is a
short reviewable queue.

The properties worth pinning are the ones that would rot silently: that a
mapped skill must exist in the dictionary, that the table stays additive over
the dictionary rather than duplicating it, and that roles and subjects never
leak into skill matching.
"""

import textwrap

import pytest

from courses import tag_map
from courses.tag_map import ROLE, SKILL, SUBJECT, TagMapError
from skills.dictionary import ALL_SKILLS


@pytest.fixture(autouse=True)
def _reset_cache():
    """The module memoises the parsed table; tests load their own."""
    tag_map._TABLE = None
    yield
    tag_map._TABLE = None


def _write(tmp_path, body: str):
    p = tmp_path / "tag_map.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ── the shipped table ─────────────────────────────────────────────────────


def test_shipped_table_is_valid():
    """Loads, and every mapped skill exists in the dictionary."""
    table = tag_map._load()
    assert table["skills"], "no tags map to a skill"
    for name in table["skills"].values():
        assert name in ALL_SKILLS


def test_shipped_table_classifies_the_three_kinds():
    assert tag_map.classify("azure virtual machines") == SKILL
    assert tag_map.classify("administrator") == ROLE
    assert tag_map.classify("Mathematics") == SUBJECT
    assert tag_map.classify("dynamics 365") is None, "unmapped must stay None"


def test_classification_ignores_case_and_surrounding_space():
    assert tag_map.skill_for("  GITHUB ") == tag_map.skill_for("github")
    assert tag_map.classify("ADMINISTRATOR") == ROLE


# ── it cannot invent skills ───────────────────────────────────────────────


def test_a_skill_the_dictionary_does_not_know_is_a_hard_error(tmp_path):
    p = _write(tmp_path, """
        skill:
          sharepoint: SharePoint
    """)
    with pytest.raises(TagMapError, match="not in skills.dictionary"):
        tag_map._load(p)


def test_roles_and_subjects_never_yield_a_skill():
    for tag in ("administrator", "business user", "Mathematics", "Humanities"):
        assert tag_map.skill_for(tag) is None
    assert tag_map.skills_for(["administrator", "Mathematics"]) == []


# ── additive over the dictionary, not a duplicate of it ───────────────────


def test_a_tag_the_dictionary_already_resolves_needs_no_entry():
    """`azure` is not in the table; the dictionary answers for it."""
    assert "azure" not in tag_map._load()["skills"]
    assert tag_map.skill_for("azure") == "Azure"
    assert tag_map.classify("azure") == SKILL


def test_table_may_not_contradict_the_dictionary(tmp_path):
    p = _write(tmp_path, """
        role:
          - azure
    """)
    with pytest.raises(TagMapError, match="dictionary already resolves"):
        tag_map._load(p)


# ── malformed input is fatal, never skipped ───────────────────────────────


def test_unknown_section_is_rejected(tmp_path):
    p = _write(tmp_path, """
        skill: {}
        producths: []
    """)
    with pytest.raises(TagMapError, match="unknown section"):
        tag_map._load(p)


def test_a_tag_in_two_kinds_is_rejected(tmp_path):
    p = _write(tmp_path, """
        role:
          - maker
        subject:
          - maker
    """)
    with pytest.raises(TagMapError, match="listed twice"):
        tag_map._load(p)


def test_a_tag_both_skill_and_role_is_rejected(tmp_path):
    p = _write(tmp_path, """
        skill:
          azure virtual machines: Azure
        role:
          - azure virtual machines
    """)
    with pytest.raises(TagMapError, match="both skill and role"):
        tag_map._load(p)


def test_a_missing_file_is_fatal(tmp_path):
    with pytest.raises(TagMapError, match="missing"):
        tag_map._load(tmp_path / "nope.yaml")


# ── ordering and dedup ────────────────────────────────────────────────────


def test_skills_for_dedupes_and_keeps_first_seen_order():
    got = tag_map.skills_for(
        ["azure devops", "administrator", "github", "azure virtual machines",
         "azure cloud services"]
    )
    assert got == ["CI/CD", "Git", "Azure"], got


# ── the report is the curation queue ──────────────────────────────────────


def test_report_counts_every_kind_and_lists_the_unclassified():
    out = tag_map.report(
        {"azure virtual machines": 10, "administrator": 5,
         "Mathematics": 3, "dynamics 365": 7}
    )
    assert "skill" in out and "role" in out and "subject" in out
    assert "UNCLASSIFIED" in out
    assert "dynamics 365" in out, "the queue must name what still needs a decision"
    assert "7" in out, "ordered by how much signal each carries"
