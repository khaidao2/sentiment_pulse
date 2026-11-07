"""A role family is a population a demand profile gets computed over.

So the hazards worth pinning are the ones that would put a posting in the wrong
population silently:

  * first-match classification, which reports one tidy family for a title that
    names two and hides the ambiguity. The earlier prototype did exactly this;
  * seniority leaking into the family, which is what produced a "Mgmt" bucket
    holding engineering and product managers averaged together;
  * UNKNOWN or AMBIGUOUS being quietly absorbed into a family, which turns a
    guess into a confident number;
  * Vietnamese titles -- 34.3% of the corpus -- silently falling out.
"""

import textwrap

import pytest

import roles.classify as rc
from roles.classify import (
    AMBIGUOUS,
    METHOD,
    QUALIFIER,
    ROLE,
    UNKNOWN,
    Classification,
    RoleRulesError,
    classify,
)


@pytest.fixture(autouse=True)
def _reset():
    rc._RULES = None
    yield
    rc._RULES = None


def _rules(tmp_path, body: str):
    p = tmp_path / "rules.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ── every rule is evaluated; there is no first-match ──────────────────────────


def test_a_title_naming_two_families_is_ambiguous_not_the_first_one():
    c = classify("Fullstack Developer / QA Tester")
    assert c.family == AMBIGUOUS
    assert {f for f, _, _ in c.matched} == {"Fullstack", "QA"}


def test_matched_carries_every_rule_that_fired_for_audit():
    c = classify("Senior Data Engineer")
    assert c.matched == (("Data", "data engineer", ROLE),)
    assert c.method == METHOD


def test_a_title_naming_nothing_is_unknown():
    c = classify("Chuyên viên kinh doanh")
    assert c.family == UNKNOWN
    assert c.matched == ()


@pytest.mark.parametrize("title", ["", "   ", None])
def test_an_empty_title_is_unknown_not_an_error(title):
    assert classify(title).family == UNKNOWN


# ── UNKNOWN and AMBIGUOUS are never usable as a target ────────────────────────


def test_only_a_real_family_is_usable():
    assert classify("Senior Data Engineer").is_usable
    assert not Classification(UNKNOWN, (), None).is_usable
    assert not Classification(AMBIGUOUS, (), None).is_usable


# ── Vietnamese is first-class, not a fallback ─────────────────────────────────


@pytest.mark.parametrize("title,family", [
    ("Chuyên viên phân tích dữ liệu", "Data"),
    ("CHUYÊN VIÊN PHÂN TÍCH DỮ LIỆU", "Data"),   # the corpus really is shouting
    ("Nhân viên IT", "IT Support"),
    ("Kỹ sư hệ thống", "DevOps"),
])
def test_vietnamese_titles_classify(title, family):
    assert classify(title).family == family


# ── seniority never decides a family ──────────────────────────────────────────


def test_seniority_is_parsed_but_does_not_create_a_family():
    c = classify("Engineering Manager")
    assert c.seniority == "manager"
    assert c.family == UNKNOWN, "manager is how senior, not what they do"


def test_seniority_does_not_change_which_family_is_chosen():
    for title in ("Data Engineer", "Senior Data Engineer", "Junior Data Engineer"):
        assert classify(title).family == "Data"


def test_the_most_senior_marker_wins():
    assert classify("Senior Engineering Manager").seniority == "manager"
    assert classify("Senior Data Engineer").seniority == "senior"


def test_a_title_with_no_marker_reports_none_rather_than_guessing():
    assert classify("Data Engineer").seniority is None


# ── a malformed rule table is fatal, never skipped ────────────────────────────


def test_a_pattern_in_two_families_is_a_rule_bug_and_is_rejected(tmp_path):
    p = _rules(tmp_path, """
        families:
          Data:
            - data engineer
          Backend:
            - data engineer
    """)
    with pytest.raises(RoleRulesError, match="is in both"):
        rc._load(p)


def test_a_reserved_outcome_cannot_be_used_as_a_family_name(tmp_path):
    p = _rules(tmp_path, """
        families:
          UNKNOWN:
            - whatever
    """)
    with pytest.raises(RoleRulesError, match="reserved outcome"):
        rc._load(p)


def test_a_family_with_no_patterns_is_rejected(tmp_path):
    p = _rules(tmp_path, "families:\n  Data: []\n")
    with pytest.raises(RoleRulesError, match="no patterns"):
        rc._load(p)


def test_an_unknown_section_is_rejected(tmp_path):
    p = _rules(tmp_path, "families:\n  Data: [data engineer]\nroless: {}\n")
    with pytest.raises(RoleRulesError, match="unknown section"):
        rc._load(p)


def test_a_missing_rules_file_is_fatal(tmp_path):
    with pytest.raises(RoleRulesError, match="missing"):
        rc._load(tmp_path / "nope.yaml")


# ── the shipped table ─────────────────────────────────────────────────────────


def test_shipped_rules_load_and_every_family_has_patterns():
    r = rc._load()
    assert r["families"], "no families defined"
    for fam, pats in r["families"].items():
        assert pats, f"{fam} has no patterns"
        assert fam not in (UNKNOWN, AMBIGUOUS)


def test_report_names_the_threshold_and_the_curation_queue():
    out = rc.report(["Data Engineer", "Chuyên viên kinh doanh", "Fullstack QA"], min_postings=1)
    assert "UNKNOWN" in out and "AMBIGUOUS" in out
    assert "chuyên viên kinh doanh" in out, "the queue must name what needs a rule"
    assert METHOD in out, "a report must say which rule version produced it"


# ── role beats qualifier; a language never outranks a job title ───────────────

# These three titles are the reason the tier exists. Before it, all three
# collided as Backend + Fullstack and the two clear ones were lost to AMBIGUOUS.
@pytest.mark.parametrize("title,family", [
    ("Senior Fullstack PHP Developer", "Fullstack"),
    ("Fullstack Java Developer (Good At English)", "Fullstack"),
])
def test_a_role_word_beats_a_language_qualifier(title, family):
    c = classify(title)
    assert c.family == family
    # The losing evidence is still REPORTED, never discarded -- an auditor must
    # be able to see that Backend matched and why it did not win.
    assert ("Backend", "php developer", QUALIFIER) in c.matched or \
           ("Backend", "java developer", QUALIFIER) in c.matched


def test_two_role_words_stay_ambiguous_even_with_the_tier():
    """The genuine hybrid must survive sharpening, or the fix went too far."""
    c = classify("Fullstack Developer (Back-end)")
    assert c.family == AMBIGUOUS
    assert {t for _, _, t in c.matched} == {ROLE}


def test_a_qualifier_alone_still_classifies():
    """12 of 79 Backend postings are named only by their language."""
    c = classify("Java Developer")
    assert c.family == "Backend"
    assert c.matched == (("Backend", "java developer", QUALIFIER),)


def test_two_qualifiers_from_different_families_are_ambiguous():
    c = classify("React Native / Angular Developer")
    assert c.family == AMBIGUOUS


def test_a_flat_list_is_shorthand_for_all_role_patterns(tmp_path):
    p = _rules(tmp_path, """
        families:
          Data:
            - data engineer
    """)
    assert rc._load(p)["families"]["Data"][ROLE] == ["data engineer"]


def test_an_unknown_tier_is_rejected(tmp_path):
    p = _rules(tmp_path, """
        families:
          Data:
            rolle:
              - data engineer
    """)
    with pytest.raises(RoleRulesError, match="unknown tier"):
        rc._load(p)


def test_a_pattern_duplicated_across_tiers_of_two_families_is_rejected(tmp_path):
    p = _rules(tmp_path, """
        families:
          Backend:
            qualifier:
              - java developer
          Fullstack:
            role:
              - java developer
    """)
    with pytest.raises(RoleRulesError, match="is in both"):
        rc._load(p)


# ── the original title is retained for audit ──────────────────────────────────


def test_the_raw_title_is_kept_exactly_as_crawled():
    raw = "  CHUYÊN VIÊN PHÂN TÍCH DỮ LIỆU  "
    c = classify(raw)
    assert c.title == raw, "normalize() casefolds; an auditor needs the original"
    assert c.family == "Data"


# ── Vietnamese normalisation preserves meaning ────────────────────────────────


def test_normalisation_does_not_strip_diacritics():
    """`dữ liệu` (data) must not collapse onto an unaccented spelling: folding
    them together would merge genuinely different Vietnamese words."""
    from skills.extractor import normalize as n
    assert n("dữ liệu") != n("du lieu")
    assert n("kỹ sư") != n("ky su")


def test_decomposed_and_composed_vietnamese_agree():
    """The crawlers see both NFC and NFD; the same title must classify the same."""
    import unicodedata
    t = "Chuyên viên phân tích dữ liệu"
    assert classify(unicodedata.normalize("NFD", t)).family == \
           classify(unicodedata.normalize("NFC", t)).family == "Data"


# ── reproducibility ───────────────────────────────────────────────────────────


def test_classification_is_deterministic_and_order_independent():
    """Same input, same verdict -- no dict-ordering or rule-ordering effects."""
    title = "Senior Fullstack PHP Developer"
    first = classify(title)
    for _ in range(5):
        rc._RULES = None          # force a reload; ordering must not matter
        assert classify(title) == first
