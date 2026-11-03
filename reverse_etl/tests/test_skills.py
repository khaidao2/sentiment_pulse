"""Extraction is precision-first: an unrecognised value is dropped, never guessed.

Cases here are taken from the live data, not invented — the polluted strings are
real values from `silver_current`.
"""

import pytest

from skills.extractor import LOOKUP, extract_skills, match_exact, normalize


# 1 — English JD
def test_extracts_from_english_jd():
    jd = "You will build data pipelines with Python, Apache Airflow and dbt on Kubernetes."
    assert extract_skills(description=jd) == ["Apache Airflow", "Kubernetes", "Python", "dbt"]


# 2 — Vietnamese JD
def test_extracts_from_vietnamese_jd():
    jd = ("Yêu cầu: thành thạo Python và PostgreSQL, có kinh nghiệm triển khai "
          "Docker, hiểu biết về Kubernetes và CI/CD.")
    got = extract_skills(description=jd)
    assert set(got) == {"Python", "PostgreSQL", "Docker", "Kubernetes", "CI/CD"}


# 3 — aliases and casing
@pytest.mark.parametrize("raw, canonical", [
    ("postgres", "PostgreSQL"), ("PostgreSql", "PostgreSQL"), ("psql", "PostgreSQL"),
    ("k8s", "Kubernetes"), ("NodeJS", "Node.js"), ("node js", "Node.js"),
    ("golang", "Go"), ("kafka", "Apache Kafka"), ("  ReactJS  ", "React"),
    ("CI CD", "CI/CD"),
])
def test_alias_and_casing_normalisation(raw, canonical):
    assert match_exact(raw) == canonical


# 4 — compound names survive intact
@pytest.mark.parametrize("raw", ["C++", "C#", ".NET", "CI/CD", "Node.js", "Spring Boot", "Ruby on Rails"])
def test_compound_names_not_split(raw):
    """A compound name round-trips to itself rather than losing a fragment."""
    assert match_exact(raw) == raw


def test_longer_term_wins_over_shorter():
    # The span is consumed by "Spring Boot", so bare "Spring" must NOT appear.
    # Order from the free-text pass follows term length, so assert membership.
    got = extract_skills(description="Spring Boot microservices")
    assert set(got) == {"Spring Boot", "Microservices"}
    assert "Spring" not in got


def test_dotted_name_does_not_yield_its_prefix():
    assert extract_skills(description="ASP.NET only") == ["ASP.NET"]


# 5 — paragraphs must not become skills
def test_requirement_paragraph_is_scanned_not_stored():
    para = ("Cử nhân chuyên ngành kinh tế, tài chính, ngân hàng, khoa học dữ liệu, "
            "khoa học máy tính, thống kê, Toán tin... hoặc các chuyên ngành liên quan "
            "- Ưu tiên có chứng chỉ hoặc có kinh nghiệm sử dụng")
    # No dictionary term appears, so nothing is stored — the paragraph itself never is.
    assert extract_skills(requirements=[para]) == []
    assert match_exact(para) is None


@pytest.mark.parametrize("junk", [
    "Ai Yêu Miền Bắc hơn MBers?", "MB Careers", "Và các Fanpage:", "Ưu tiên:",
    "2. Kinh nghiệm", "Chế độ đãi ngộ", "Từ 3 - 5 kỹ năng nổi bật", "Kinh nghiệm làm việc",
])
def test_real_polluted_values_are_rejected(junk):
    """Every one of these is a real current value in the skills table."""
    assert match_exact(junk) is None


# 6 — several skills in one field
def test_multiple_skills_in_one_value():
    assert set(extract_skills(requirements=["Python, SQL and Docker"])) == {"Python", "SQL", "Docker"}


# 7 — empty and malformed
@pytest.mark.parametrize("bad", [None, "", "   ", "-", "•", "...", "1.", ":"])
def test_empty_and_malformed(bad):
    assert match_exact(bad) is None
    assert extract_skills(tech_stack=[bad]) == []


def test_no_inputs_at_all():
    assert extract_skills() == []


# 8 — duplicates collapse, order is stable
def test_duplicates_collapse_across_sources():
    got = extract_skills(
        tech_stack=["Python", "python", "PostgreSql"],
        description="Python and postgres everywhere",
        requirements=["python"],
    )
    assert got == ["Python", "PostgreSQL"]


# 9 — exclusions are decisions, not gaps
@pytest.mark.parametrize("role", ["Backend Developer", "Fullstack Developer", "Tester", "lập trình viên", "Data Engineer"])
def test_roles_never_become_skills(role):
    assert match_exact(role) is None


@pytest.mark.parametrize("lang", ["English", "Japanese", "Tiếng Anh"])
def test_languages_kept_out_of_technical_skills(lang):
    assert match_exact(lang) is None


@pytest.mark.parametrize("generic", ["AI", "Cloud", "Database", "Security", "IT"])
def test_generic_categories_rejected(generic):
    assert match_exact(generic) is None


def test_role_in_tech_stack_is_dropped_but_real_skills_kept():
    got = extract_skills(tech_stack=["Backend Developer", "English", "Python", "AI"])
    assert got == ["Python"]


# 10 — idempotency
def test_repeated_extraction_is_identical():
    args = dict(tech_stack=["Python", "k8s"], description="We run Docker and Apache Kafka.",
                requirements=["Experience with PostgreSQL"])
    first = extract_skills(**args)
    assert first == extract_skills(**args) == extract_skills(**args)


def test_normalize_is_stable_under_repetition():
    once = normalize("  • PostgreSql.  ")
    assert once == normalize(once) == "postgresql"


# professional skills — stored, but a distinct class from technologies
@pytest.mark.parametrize("raw, canonical", [
    ("Project Management", "Project Management"), ("project mgmt", "Project Management"),
    ("quản lý dự án", "Project Management"), ("Team Management", "Team Management"),
    ("people management", "Team Management"), ("Leadership", "Leadership"),
    ("lãnh đạo", "Leadership"), ("Stakeholder Management", "Stakeholder Management"),
])
def test_professional_skills_are_recognised(raw, canonical):
    assert match_exact(raw) == canonical


def test_professional_and_technical_are_distinguishable():
    from skills.dictionary import kind
    assert kind("Project Management") == "professional"
    assert kind("Python") == "technical"


def test_professional_skill_extracted_from_prose():
    got = extract_skills(description="Strong leadership and stakeholder management across teams")
    assert set(got) == {"Leadership", "Stakeholder Management"}


def test_adding_professional_skills_did_not_admit_roles_or_languages():
    """The four additions must not have widened the exclusions by accident."""
    for v in ["Project Manager", "Manager", "English", "Backend Developer"]:
        assert match_exact(v) is None


def test_every_alias_resolves_to_a_canonical_name():
    from skills.dictionary import ALL_SKILLS
    assert set(LOOKUP.values()) <= set(ALL_SKILLS)


def test_no_canonical_name_is_also_excluded():
    """A term cannot be both a skill and an exclusion — that would be ambiguous."""
    from skills.dictionary import ALL_SKILLS, EXCLUDED
    assert not {normalize(c) for c in ALL_SKILLS} & EXCLUDED


# ── scannability: which terms may be looked for inside prose ────────────────


def test_symbol_bearing_short_terms_are_scannable():
    """"C#" is two characters and unambiguous; the length floor alone hid it.

    Regression: before `is_scannable` the rule was `len(t) >= 3`, so no
    description could ever yield C# -- a language 50+ postings name.
    """
    from skills.extractor import is_scannable
    assert is_scannable("C#")
    assert extract_skills(description="Backend in C# and .NET Core") == [".NET Core", "C#"]


def test_ambiguous_short_terms_stay_exact_match_only():
    """The floor exists for these; a symbol does not rescue a real word."""
    from skills.extractor import is_scannable
    for term in ("c", "go", "ml", "js", "py", "bi", "cv"):
        assert not is_scannable(term), term
    assert extract_skills(description="We go to the office; a grade of C is fine") == []


def test_scannability_agrees_with_the_scanner_it_builds():
    """`is_scannable` IS the scanner's membership rule, not a parallel one.

    The web application reads this answer per term out of `skill_terms` and
    must not have to re-derive it; if the two could disagree, a CV and a job
    posting would be read with different vocabularies.
    """
    from skills.extractor import LOOKUP, SCANNER, is_scannable
    assert len(SCANNER) == sum(1 for t in LOOKUP if is_scannable(t))


# ── the shared scan contract ────────────────────────────────────────────────
#
# These cases are DUPLICATED, verbatim, in the web repository
# (backend/tests/application/services/test_skill_scan.py::SHARED_CASES).
#
# The product is a set operation between a job posting read HERE and a CV read
# THERE, which is meaningless if the two are read by different rules. The
# repositories deploy separately and neither can import the other, so the
# contract is pinned by identical cases on both sides: a change to one scanner
# that is not made to the other fails a test somewhere.
#
# If you change a case here, change it there in the same breath.

import pytest as _pytest

SHARED_CASES = [
    ("Ran k8s in production with postgres behind it", {"Kubernetes", "PostgreSQL"}),
    ("Built the frontend in reactjs", {"React"}),
    ("Wrote services in golang", {"Go"}),
    ("Spring Boot services", {"Spring Boot"}),
    ("Hosted on ASP.NET", {"ASP.NET"}),
    ("Backend in C# and some C++", {"C#", "C++"}),
    ("We go to the office; I got a C in maths", set()),
    ("py is my favourite extension", set()),
    ("Ｐｙｔｈｏｎ daily", {"Python"}),
    ("Everything ships through CI/CD.", {"CI/CD"}),
]


@_pytest.mark.parametrize("text,expected", SHARED_CASES)
def test_the_shared_scan_contract(text, expected):
    assert set(extract_skills(description=text)) == expected


# ── a leading dot is a name, not decoration ─────────────────────────────────


def test_a_leading_dot_survives_when_a_letter_follows_it():
    """".NET" is a name; "1. " and "• " are list markers.

    Stripping the dot unconditionally normalised ".NET" to "net", which left
    the skill with no term matching its own display name AND made the
    three-letter string "net" scannable.
    """
    assert normalize(".NET") == ".net"
    assert normalize(".NET Core") == ".net core"
    assert normalize("ASP.NET") == "asp.net"


@pytest.mark.parametrize("decorated,plain", [
    ("1. Python", "python"), ("• Docker", "docker"), ("- Kubernetes", "kubernetes"),
    ("2) Linux", "linux"), ("  .NET  ", ".net"),
])
def test_list_decoration_is_still_stripped(decorated, plain):
    assert normalize(decorated) == plain


def test_net_profit_is_not_the_dotnet_framework():
    """The false positive the old rule allowed.

    Latent rather than live -- measured on the corpus, 0 of 83 .NET edges came
    from a bare "net" -- but a latent false positive in a demand figure is
    still a wrong number waiting to happen.
    """
    assert extract_skills(description="Our net profit grew 20% this year") == []
    assert extract_skills(description="A safety net for the team") == []


def test_dotnet_itself_still_extracts():
    assert extract_skills(description="We use .NET every day") == [".NET"]
    assert extract_skills(description="Built on .NET Core") == [".NET Core"]
