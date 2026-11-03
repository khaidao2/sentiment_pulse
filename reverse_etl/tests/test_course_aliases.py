"""The aliases added for the course catalogue, and the ones deliberately refused.

Course `teaches` requires the skill in the TITLE, so the dictionary's aliases
decide what the catalogue is able to teach. The 2026-11-02 crawl's coverage
audit found the gap is mostly a genuinely absent course — no title in 7,205 says
AWS, Kafka, Scrum, Jira, Figma or Selenium — but a handful of skills were
present under a phrasing the dictionary did not know. These tests pin both
halves: the phrasings now matched, and the far larger set deliberately NOT
matched, because the refusals are the entries a future curator is most likely
to "fix" and quietly inflate coverage with.
"""

import pytest

from skills import extract_skills
from skills.dictionary import ALL_SKILLS


def teaches(title: str) -> list[str]:
    """What a course with this title teaches — the same call match.py makes."""
    return extract_skills(description=title)


# ── phrasings the catalogue actually uses ────────────────────────────────────

@pytest.mark.parametrize("title,skill", [
    # CI/CD is named by its products, never by the acronym, in these titles.
    ("Introduction to GitHub Actions", "CI/CD"),
    ("Create and publish custom GitHub actions", "CI/CD"),
    ("Explore Azure Pipelines", "CI/CD"),
    # Singular, which is how MS Learn writes it.
    ("Build your first microservice with .NET", "Microservices"),
    # ETL appears in no title under that name; Data Factory is the one product
    # in this catalogue that unambiguously teaches it.
    ("Orchestrate data movement and transformation in Azure Data Factory", "ETL"),
    ("Petabyte-scale ingestion with Azure Data Factory", "ETL"),
    # The catalogue names the artefact where the dictionary names the activity.
    ("Build your data model with Microsoft Dataverse", "Data Modeling"),
    ("Build data models in finance and operations apps", "Data Modeling"),
    # normalize() folds case and whitespace but not hyphens.
    ("Introduction to retrieval-augmented generation concepts", "RAG"),
    ("Get started with data warehouses in Microsoft Fabric", "Data Warehousing"),
])
def test_catalogue_phrasing_is_matched(title, skill):
    assert skill in teaches(title)


# ── phrasings deliberately refused ───────────────────────────────────────────

@pytest.mark.parametrize("title,skill", [
    # "github" is 124 titles on this crawl: 52 GitHub Copilot, 53 Packages /
    # Codespaces / enterprise admin. None of them teach version control, so
    # github -> Git would be the single largest false mapping available.
    ("Consolidate duplicate logic using GitHub Copilot Agent", "Git"),
    ("Manage GitHub Actions in the enterprise", "Git"),
    # "copilot" is 325 titles and would take Generative AI from 36 to ~360.
    # Using a Microsoft product is not the market skill "Generative AI".
    ("Understand the business impact of Copilot+ PCs", "Generative AI"),
    ("Navigate your Microsoft Copilot experience", "Generative AI"),
    # Azure Container Apps is a managed service; it is not Docker.
    ("Deploy a Spring Boot app to Azure Container Apps", "Docker"),
    # Bicep is Azure's own IaC language, not Terraform, and there is no
    # canonical "Infrastructure as Code" skill to map either one onto.
    ("Fundamentals of Bicep", "Terraform"),
    ("Implement security controls in infrastructure as code", "Terraform"),
    # "api" is in dictionary.GENERIC by decision; a Dataverse Web API course is
    # not a REST API course.
    ("Work with Dataverse Web API", "REST API"),
    # The boundary rules already refuse these; pinned so a widened alias cannot
    # silently undo them.
    ("Nuclear Reactor Safety", "React"),
    ("Secure endpoints with Zero Trust", "Rust"),
    ("Introduction To Higher Order Functions And Callbacks In Javascript", "Java"),
])
def test_refused_phrasing_stays_refused(title, skill):
    assert skill not in teaches(title)


# ── the rules the additions must not break ───────────────────────────────────

def test_new_aliases_name_only_existing_canonical_skills():
    """No alias invented a skill; every one hangs off a name already stored."""
    for name in ["CI/CD", "Microservices", "ETL", "Data Modeling", "RAG",
                 "Data Warehousing"]:
        assert name in ALL_SKILLS


def test_product_aliases_do_not_also_yield_their_vendor_prefix():
    """Documented cost of the Azure-prefixed aliases, pinned so it stays known.

    The extractor consumes a matched span, so the longer product name wins and
    "Explore Azure Pipelines" no longer teaches Azure. That is 13 of Azure's
    740 course edges. Accepted on the trade, but recorded here rather than left
    to surprise the next reader of the coverage report.
    """
    assert teaches("Explore Azure Pipelines") == ["CI/CD"]
    assert "Azure" not in teaches("Integrate data with Azure Data Factory")
    # ...while a plain Azure title is untouched.
    assert "Azure" in teaches("Introduction to Azure Advisor")


def test_mentions_phrasing_is_not_promoted_by_these_aliases():
    """An alias only changes what a TITLE matches; the relation split is match.py's.

    Cheap guard that the new multi-word aliases did not become so loose that a
    title naming the vendor alone now claims the product's skill.
    """
    assert "CI/CD" not in teaches("Introduction to GitHub")
    assert "ETL" not in teaches("Introduction to Azure Data Studio")
