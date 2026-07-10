-- CONTRACT — the `jobs` table the WEB app owns in ITS Postgres.
--
-- The web owns this table and its migrations. The reverse-ETL DAG in this repo
-- only UPSERTs into it (keyed on job_id); it never creates or alters it. This
-- file is the agreed shape both sides code against — copy it into the web repo's
-- first migration.
--
-- Columns mirror silver_current (ClickHouse). Arrays land as Postgres text[].
CREATE TABLE IF NOT EXISTS jobs (
    job_id                text PRIMARY KEY,   -- sha256(job_url), the natural key
    job_url               text,
    title                 text NOT NULL,
    company_name          text,
    company_url           text,
    salary_type           text,
    salary_min_usd        integer,
    salary_max_usd        integer,
    currency              text,
    location              text,
    work_arrangement      text,
    posted_days_ago       integer,
    company_industry      text,
    country               text,
    min_years_experience  integer,
    tech_stack            text[],
    description           text,
    skills                text[],
    benefits              text[],
    crawled_at            timestamptz,        -- version: time of last content change
    source                text,               -- origin site, e.g. 'itviec'
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_crawled_at_idx ON jobs (crawled_at);
