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


def test_every_alias_resolves_to_a_canonical_name():
    from skills.dictionary import CANONICAL
    assert set(LOOKUP.values()) <= set(CANONICAL)


def test_no_canonical_name_is_also_excluded():
    """A term cannot be both a skill and an exclusion — that would be ambiguous."""
    from skills.dictionary import CANONICAL, EXCLUDED
    assert not {normalize(c) for c in CANONICAL} & EXCLUDED
