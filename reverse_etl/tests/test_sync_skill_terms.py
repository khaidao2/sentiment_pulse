"""The vocabulary export: every spelling, each with its own scannability."""

from skills.dictionary import ALL_SKILLS
from skills.extractor import LOOKUP, normalize
from sync_skill_terms import terms


def test_every_dictionary_spelling_is_exported():
    """Nothing the crawler can match may be missing from what the app gets.

    If these two ever diverge, a job posting and a CV are read with different
    vocabularies -- and the whole product is a set operation between them.
    """
    assert {t for t, _, _ in terms()} == set(LOOKUP)


def test_canonical_names_are_terms_too():
    """One table to match against. A consumer never unions this with `skills`."""
    exported = {t: c for t, c, _ in terms()}
    for canonical in ALL_SKILLS:
        assert exported[normalize(canonical)] == canonical


def test_every_term_points_at_a_canonical_name():
    assert {c for _, c, _ in terms()} <= set(ALL_SKILLS)


def test_terms_are_stored_normalised():
    """The consumer compares against this form and must not re-normalise."""
    for term, _, _ in terms():
        assert term == normalize(term)


def test_scannability_matches_the_crawlers_own_rule():
    from skills.extractor import is_scannable
    for term, _, scannable in terms():
        assert scannable == is_scannable(term), term


def test_ambiguous_terms_are_exported_but_not_scannable():
    """"go" must reach the app -- as an exact match only, never a prose scan."""
    flags = {t: s for t, _, s in terms()}
    assert flags["go"] is False
    assert flags["c#"] is True
    assert flags["k8s"] is True
