"""Curated technical-skill dictionary.

Seeded from the live `technologies` table (1875 distinct values) and then
curated by hand. The seed is noisy in a specific way: 1261 of those values occur
exactly once and the long tail is job titles, including 91-character Vietnamese
ones. So the canon below is drawn from the head of that distribution, and the
three exclusion sets record WHY a frequent value is not a skill — an entry in
EXCLUDED is a decision, not an oversight, and keeps the next curator from
re-adding it.

CANONICAL maps the stored name to the strings that mean it. Aliases are matched
case-insensitively against normalised text; the key is the display form.
"""

# ── canonical technical skills ───────────────────────────────────────────────
# {display name: [aliases]} — the canonical name itself is always matched, so
# aliases only list the OTHER spellings seen in the data.
CANONICAL: dict[str, list[str]] = {
    # languages (programming)
    "Python": ["py"],
    "Java": [],
    "JavaScript": ["js", "java script"],
    "TypeScript": ["ts"],
    "C++": ["cpp", "c plus plus"],
    "C#": ["c sharp", "csharp"],
    "C": ["c language"],
    "Go": ["golang"],
    "PHP": [],
    "Ruby": [],
    "Kotlin": [],
    "Swift": [],
    "Scala": [],
    "Rust": [],
    "SQL": [],
    "Bash": ["bash shell", "shell script", "shell scripting"],
    "PowerShell": ["power shell"],
    "HTML": ["html5"],
    "CSS": ["css3"],
    # frameworks / runtimes
    "Node.js": ["nodejs", "node js"],
    "React": ["reactjs", "react js"],
    "React Native": ["react-native"],
    "Angular": ["angularjs"],
    "Vue.js": ["vuejs", "vue js", "vue"],
    "Next.js": ["nextjs", "next js"],
    "NestJS": ["nest js"],
    "Spring": [],
    "Spring Boot": ["springboot"],
    ".NET": ["dotnet", "dot net"],
    ".NET Core": [".net core", "dotnet core"],
    "ASP.NET": ["asp net", "aspnet"],
    "Laravel": [],
    "Ruby on Rails": ["rails", "ror"],
    "FastAPI": ["fast api"],
    "Django": [],
    "Flask": [],
    "Unity": [],
    # data stores
    "PostgreSQL": ["postgres", "postgresql", "postgre", "psql", "postgre sql"],
    "MySQL": ["my sql"],
    "Microsoft SQL Server": ["mssql", "sql server", "ms sql server", "ms sql"],
    "Oracle": ["oracle database", "oracle db"],
    "MongoDB": ["mongo db", "mongo"],
    "Redis": [],
    "Elasticsearch": ["elastic search", "elk"],
    "NoSQL": ["no sql"],
    "ClickHouse": ["click house"],
    # platform / infra
    "Docker": [],
    "Kubernetes": ["k8s", "k8"],
    "Linux": [],
    "Windows Server": [],
    "VMware": ["vm ware"],
    "Terraform": [],
    "Ansible": [],
    "Jenkins": [],
    "Git": [],
    # "github actions" / "azure pipelines" are CI/CD PRODUCTS, not a second
    # skill. A title that names one is teaching CI/CD and nothing else in the
    # dictionary covers it; tag_map.yaml already maps the `azure devops` TAG to
    # CI/CD, and a title is the stronger evidence of the two. Worth 19 courses
    # on the 2026-11-02 crawl. Note what is NOT here: bare "github", which on
    # this catalogue is 124 titles of which 52 are GitHub Copilot and 53 are
    # Packages/Codespaces/admin — none of them CI/CD, and none of them Git.
    # KNOWN COST: the extractor consumes a matched span, so "Explore Azure
    # Pipelines" now teaches CI/CD and no longer Azure. That costs Azure 13 of
    # 740 course edges and is accepted on the trade — CI/CD went 13 -> 31 and
    # ETL 0 -> 7 — but it is a real loss, not a free win.
    "CI/CD": ["ci cd", "cicd", "continuous integration", "continuous delivery",
              "github actions", "github action", "azure pipelines"],
    "AWS": ["amazon web services"],
    "Azure": ["microsoft azure"],
    "GCP": ["google cloud platform", "google cloud"],
    # Singular: MS Learn consistently writes "Build your first microservice".
    "Microservices": ["micro services", "microservices architecture", "microservice"],
    # data / ML
    "Apache Kafka": ["kafka"],
    "Apache Spark": ["spark", "pyspark"],
    "Apache Airflow": ["airflow"],
    "dbt": ["data build tool"],
    # Azure Data Factory is Microsoft's ETL service and the only thing in this
    # catalogue that teaches ETL by any name; ETL itself appears in no title.
    # This is a product -> skill inference and the weakest entry in this file,
    # so it is kept to the one unambiguous product (7 courses) rather than
    # widened to "data integration" or "data pipeline", which name the concept
    # and would also drag in Dynamics ERP record-sync courses.
    "ETL": ["azure data factory"],
    "Power BI": ["powerbi"],
    "Machine Learning": ["ml"],
    "Deep Learning": [],
    "Computer Vision": ["cv"],
    "NLP": ["natural language processing"],
    "MLOps": ["ml ops"],
    "Generative AI": ["gen ai", "genai"],
    "LLM": ["large language model", "large language models"],
    # Hyphenated is how it is actually written in prose; normalize() folds case
    # and whitespace but not hyphens, so the unhyphenated alias alone missed it.
    "RAG": ["retrieval augmented generation", "retrieval-augmented generation"],
    "Data Warehousing": ["data warehouse", "data warehouses", "datawarehouse"],
    "Data Mining": [],
    # The catalogue names the artefact ("Build your data model with Dataverse")
    # where the dictionary names the activity. 11 courses; one of them,
    # "Microsoft for Healthcare data model for Dataverse", describes a shipped
    # model rather than teaching modelling, and is the known cost of this entry.
    "Data Modeling": ["data modelling", "data model", "data models"],
    "Big Data": [],
    "Business Intelligence": ["bi"],
    # testing / quality
    "Selenium": [],
    "Playwright": [],
    "Appium": [],
    "Automation Testing": ["automation test", "test automation"],
    "Manual Testing": ["manual test"],
    "Penetration Testing": ["pentest", "pen testing"],
    # tooling / design
    "Jira": [],
    "Figma": [],
    "Adobe Illustrator": ["illustrator"],
    "Adobe XD": [],
    "Odoo": [],
    "SAP": [],
    "ERP": [],
    "CRM": [],
    "BPMN": [],
    "JSON": [],
    "REST API": ["restful api", "rest apis", "restful"],
    "GraphQL": ["graph ql"],
    # mobile / embedded
    "Android": [],
    "iOS": [],
    "AUTOSAR": [],
    "Embedded C": [],
    # practice / architecture (kept: they name a distinct, searchable capability)
    "Agile": [],
    "Scrum": [],
    "OOP": ["object oriented programming", "object-oriented programming"],
    "Clean Architecture": [],
    "DevOps": [],
    "DevSecOps": ["dev sec ops"],
    "Cybersecurity": ["cyber security"],
    "Information Security": ["infosec"],
}

# ── canonical professional skills ───────────────────────────────────────────
# Real, searchable capabilities that are not technologies. Held in their own map
# so the technical/professional split is recorded in the data rather than in a
# reader's head. Both kinds are stored in the same `skills` table today, because
# the web schema has one lookup and no `kind` column — persisting the
# distinction would be a schema change, so it is deliberately NOT made here.
# `kind()` below is what a future migration would read.
PROFESSIONAL: dict[str, list[str]] = {
    "Project Management": ["project mgmt", "quản lý dự án"],
    "Team Management": ["team mgmt", "people management", "quản lý nhóm"],
    "Leadership": ["team leadership", "lãnh đạo"],
    "Stakeholder Management": ["stakeholder mgmt", "stakeholder engagement"],
}


# ── excluded: job roles ──────────────────────────────────────────────────────
# A role is what someone IS, not what they can do. The posting already carries
# it in `title`; duplicating it as a skill makes "Backend Developer" look like a
# searchable capability and pollutes skill-based matching.
ROLES: set[str] = {
    "backend developer", "frontend developer", "fullstack developer", "fullstack",
    "web developer", "web programmer", "software developer", "software engineer",
    "mobile application developer", "game developer", "advert developer",
    "erp developer", "developer", "lập trình viên", "lập trình",
    "data analyst", "data engineer", "data scientist", "business analyst",
    "software business analyst", "financial analyst", "sales analyst", "analysts",
    "devops engineer", "cloud engineer", "network engineer", "security engineer",
    "systems engineer / administrator", "network administrator", "system admin",
    "database administrator", "it administrator", "automation engineer",
    "embedded engineer", "service engineer", "testing engineer", "bridge engineer",
    "ai engineer", "ai / machine learning engineer", "ai architect",
    "solution architect", "software/technical architect", "it engineer",
    "information technology engineer", "telecommunication engineer",
    "project manager", "product manager", "product owner", "it manager",
    "it supervisor", "manager", "it specialist", "it executive", "it officer",
    "it staff", "it support", "it helpdesk", "it support assistant", "technician",
    "tester", "automation tester", "manual tester", "software tester",
    "test coordinator / qaqc coordinator", "software engineer in test (sdet)",
    "presale", "senior executive", "senior officer", "senior specialist",
    "accounts officer", "account representative", "boutique sales associate",
    "technical staff", "technical assistant", "chuyên viên it", "nhân viên it",
    "kỹ sư cntt", "kỹ thuật viên", "nhân viên kỹ thuật", "nhân viên tin học",
    "nhân viên xử lý dữ liệu", "nhân viên kiểm thử phần mềm", "nhân viên bảo trì",
    "kỹ sư điện tử viễn thông", "kỹ sư viễn thông", "kỹ thuật viên viễn thông",
    "kỹ thuật viên máy tính", "kỹ sư công nghệ thông tin", "kỹ sư hệ thống mạng",
    "nhân viên công nghệ thông tin", "nhân viên kỹ thuật cntt", "chuyên viên chính",
    "nhân viên phòng kỹ thuật", "nhân viên hỗ trợ kỹ thuật", "bridge system engineer (brse)",
}

# ── excluded: human languages ────────────────────────────────────────────────
# Real and useful for matching, but not a TECHNICAL skill. There is no language
# table in the web schema today (see the data-model note in the report), so these
# are recognised and deliberately NOT stored rather than silently folded into
# `skills` where they would rank as the single most common "technology".
LANGUAGES: set[str] = {
    "english", "japanese", "korean", "chinese", "tiếng anh", "tiếng nhật",
    "tiếng hàn", "tiếng trung", "japanese it communication",
    "it communication/translation",
}

# ── excluded: too generic to search on ───────────────────────────────────────
# These are categories, not capabilities. "Cloud" matches a job that mentions
# AWS, Azure and GCP equally, so storing it adds a row that narrows nothing.
GENERIC: set[str] = {
    "ai", "cloud", "api", "database", "security", "design", "it", "networking",
    "network system", "games", "data", "software", "hardware", "system",
    "technology", "computer", "internet", "web", "mobile", "mobile apps",
    "data processing", "data validation", "data-driven", "embedded", "electronics",
    "quality control", "qa qc", "qa/qc", "qc", "integration test", "user story",
    "brd", "algorithms", "design systems", "architecture", "firewall", "windows",
    "giao tiếp", "điện tử viễn thông", "quản trị mạng",
}

EXCLUDED: set[str] = ROLES | LANGUAGES | GENERIC

#: every stored skill, whichever kind
ALL_SKILLS: dict[str, list[str]] = {**CANONICAL, **PROFESSIONAL}


def kind(canonical: str) -> str:
    """"technical" | "professional" — the split the schema cannot hold yet."""
    if canonical in PROFESSIONAL:
        return "professional"
    if canonical in CANONICAL:
        return "technical"
    raise KeyError(canonical)
