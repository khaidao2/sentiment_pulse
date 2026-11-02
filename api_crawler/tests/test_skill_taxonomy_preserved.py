"""The internal Vietnamese skill taxonomy stays authoritative.

`reverse_etl/skills/dictionary.py` was seeded from 1875 distinct values on the
live Vietnamese job index and hand-curated down. Its Vietnamese aliases
("quản lý dự án" -> Project Management) and its Vietnamese exclusions
("kỹ sư công nghệ thông tin" is a job title, not a skill) are the asset: no
public taxonomy supplies them. ESCO carries 28 languages and Vietnamese is not
one of them; neither O*NET nor Lightcast covers it either. So a migration onto
an external taxonomy would trade precision on THIS market for breadth, and the
Vietnamese surface forms would simply be lost.

This test exists because that loss would be silent -- skill extraction would
keep returning plausible English names while quietly dropping Vietnamese
matches and re-admitting Vietnamese job titles as skills. It pins the aliases,
the exclusions, the EXCLUDED composition and the NFKC normalisation, and it
asserts the extractor needs no external taxonomy to run.

A crosswalk (internal skill -> optional external uri) is deliberately NOT
required here. If one is added later it must stay optional and additive, which
test_a_crosswalk_would_be_additive_only pins.
"""

import sys
import unicodedata
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "reverse_etl"))

from skills import dictionary as d          # noqa: E402
from skills.extractor import extract_skills, normalize  # noqa: E402


# ── Vietnamese aliases must keep resolving ────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("Cần kỹ năng quản lý dự án", "Project Management"),
    ("Yêu cầu quản lý nhóm", "Team Management"),
    ("Kỹ năng lãnh đạo tốt", "Leadership"),
])
def test_vietnamese_aliases_resolve_to_canonical_skills(text, expected):
    assert expected in extract_skills(description=text)


def test_vietnamese_aliases_are_still_declared():
    """Pinned explicitly: an alias list rewrite would otherwise pass silently."""
    pro = d.PROFESSIONAL
    assert "quản lý dự án" in pro["Project Management"]
    assert "quản lý nhóm" in pro["Team Management"]
    assert "lãnh đạo" in pro["Leadership"]


def test_code_switched_text_still_yields_english_tech_names():
    """VN tech postings are Vietnamese prose with English technology tokens."""
    got = extract_skills(description="Tuyển lập trình viên Python và Kubernetes")
    assert "Python" in got and "Kubernetes" in got
    assert "lập trình viên" not in got, "a job title is not a skill"


# ── Vietnamese exclusions must keep excluding ─────────────────────────────


@pytest.mark.parametrize("title", [
    "kỹ sư công nghệ thông tin",
    "kỹ thuật viên",
    "quản trị mạng",
])
def test_vietnamese_non_skills_stay_excluded(title):
    assert extract_skills(description=title) == []


def test_excluded_is_still_the_union_of_its_three_reasons():
    """ROLES / LANGUAGES / GENERIC each record WHY a frequent value is not a
    skill. Collapsing them would lose the reason."""
    assert d.EXCLUDED == d.ROLES | d.LANGUAGES | d.GENERIC
    assert d.EXCLUDED, "exclusions must not be emptied"


def test_vietnamese_terms_survive_in_the_exclusion_sets():
    vn = [t for t in d.EXCLUDED if any(c in t for c in "ăâđêôơưạảấầệỹỵịọ")]
    assert len(vn) >= 20, f"Vietnamese exclusions have thinned out: {len(vn)}"


# ── normalisation ─────────────────────────────────────────────────────────


def test_nfkc_normalisation_is_preserved():
    """Vietnamese pages serve full-width and decomposed forms; both must fold
    to the same key or a real skill silently stops matching."""
    composed = "quản lý dự án"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert normalize(composed) == normalize(decomposed)


def test_normalisation_protects_the_tag_path():
    """`tech_stack` tags go through match_exact -> normalize, so NFKC and NFD
    forms both resolve there."""
    nfd = unicodedata.normalize("NFD", "quản lý dự án")
    assert extract_skills(tech_stack=[nfd]) == ["Project Management"]
    assert extract_skills(tech_stack=["ｐｙｔｈｏｎ"]) == ["Python"]


def test_decomposed_vietnamese_matches_in_free_text():
    """Was a strict xfail: the free-text blob used to be scanned RAW, so a
    decomposed (NFD) Vietnamese alias never matched in the one place
    Vietnamese prose actually lives. extract_skills now NFKC-folds the blob,
    giving free text the same guarantee the tech_stack path always had."""
    nfd = unicodedata.normalize("NFD", "quản lý dự án")
    assert "Project Management" in extract_skills(description=f"Cần {nfd} tốt")


def test_free_text_gets_the_same_fold_as_tags():
    """Full-width prose folds in free text too, not only in tags."""
    assert "Python" in extract_skills(description="Tuyển ｐｙｔｈｏｎ developer")


def test_normalising_the_blob_does_not_shift_consumed_spans():
    """NFKC changes string LENGTH, and the matcher records matched spans as
    offsets. Offsets must be computed against the string actually scanned, or
    span consumption silently lets a shorter term re-claim taken text.

    The prefix here is NFD Vietnamese, so NFKC shortens it; the overlapping
    pair "Spring Boot" / "Spring" sits after the shift.
    """
    prefix = unicodedata.normalize("NFD", "Cần tuyển kỹ sư giỏi")
    assert len(prefix) != len(unicodedata.normalize("NFKC", prefix)), (
        "this test is only meaningful while NFKC changes the prefix length")
    got = extract_skills(description=f"{prefix} Spring Boot và Node.js")
    assert "Spring Boot" in got
    assert "Spring" not in got, "the shorter term re-claimed consumed text"
    assert "Node.js" in got
    assert "Node" not in got


def test_span_consumption_survives_a_lengthening_fold():
    """The other direction: the 'ﬁ' ligature makes NFKC output LONGER."""
    blob = "Chúng tôi ﬁnd Spring Boot engineers"
    assert len(blob) < len(unicodedata.normalize("NFKC", blob))
    got = extract_skills(description=blob)
    assert "Spring Boot" in got
    assert "Spring" not in got


@pytest.mark.parametrize("title", [
    "kỹ sư công nghệ thông tin",
    "kỹ thuật viên",
    "quản trị mạng",
])
def test_decomposed_vietnamese_job_titles_stay_excluded(title):
    """Folding the blob must not admit terms the taxonomy deliberately keeps
    out: an NFD job title is still a job title."""
    assert extract_skills(description=unicodedata.normalize("NFD", title)) == []
    assert extract_skills(tech_stack=[unicodedata.normalize("NFD", title)]) == []


# ── internal taxonomy remains authoritative ───────────────────────────────


def test_no_external_taxonomy_is_required():
    """The extractor must run with no ESCO/O*NET/Lightcast dependency."""
    source = (_ROOT / "reverse_etl" / "skills" / "dictionary.py").read_text(encoding="utf-8")
    source += (_ROOT / "reverse_etl" / "skills" / "extractor.py").read_text(encoding="utf-8")
    low = source.lower()
    for foreign in ("esco", "o*net", "onet", "lightcast"):
        assert f"import {foreign}" not in low
        assert f"from {foreign}" not in low


def test_canonical_names_are_the_join_key():
    """match.py and sync_jobs.py both key on these display names, so the two
    sides of a job<->course join only line up while one dictionary decides."""
    assert d.ALL_SKILLS
    assert "Python" in d.CANONICAL


def test_a_crosswalk_would_be_additive_only():
    """If an external-uri crosswalk is added, it must not become required.

    Asserted as a property of today's structure: canonical entries map a name
    to alias lists, so an optional uri can only be added alongside, never in
    place of, the Vietnamese aliases.
    """
    for name, aliases in list(d.CANONICAL.items())[:5]:
        assert isinstance(name, str)
        assert isinstance(aliases, (list, tuple, set)), (
            "aliases must stay a collection; a bare uri string would replace them")
