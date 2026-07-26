-- Runs once on first init of the compose `postgres` service (T-005).
-- The `corpus` database itself is created by the POSTGRES_DB env var; this only
-- enables pgvector. Schema + indexes are owned by Alembic (T-101).
CREATE EXTENSION IF NOT EXISTS vector;
