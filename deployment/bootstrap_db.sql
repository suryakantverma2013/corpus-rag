-- One-time bootstrap for a LOCAL Postgres instance (T-005).
--
-- Run this against the `corpus` database AFTER it exists (see README for the
-- `CREATE DATABASE corpus` step, which cannot be `IF NOT EXISTS` in plain SQL):
--
--   psql -h localhost -U postgres -d corpus -f deployment/bootstrap_db.sql
--
-- Schema + indexes are owned by Alembic (T-101); this only enables pgvector.
CREATE EXTENSION IF NOT EXISTS vector;
