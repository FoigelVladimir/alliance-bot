\set ON_ERROR_STOP on
\getenv ledger_app_password ALLIANCE_LEDGER_APP_PASSWORD
\if :{?ledger_app_password}
\else
  \echo 'Set ALLIANCE_LEDGER_APP_PASSWORD for the append/read application role.'
  \quit 1
\endif

-- Run as the maintenance owner in the separate ledger cluster, never in the working cluster.
CREATE SCHEMA IF NOT EXISTS deletion_ledger;
REVOKE ALL ON SCHEMA deletion_ledger FROM PUBLIC;
CREATE TABLE IF NOT EXISTS deletion_ledger.marks (
    environment varchar(128) NOT NULL,
    deletion_id uuid NOT NULL,
    event_uuid uuid NOT NULL,
    actor_uuid uuid NOT NULL,
    operation_id uuid NOT NULL,
    request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    notice_mode varchar(24) NOT NULL CHECK (notice_mode IN ('none', 'notify', 'already_notified')),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (environment, event_uuid),
    UNIQUE (environment, actor_uuid, operation_id)
);
ALTER TABLE deletion_ledger.marks ADD COLUMN IF NOT EXISTS deletion_id uuid NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ledger_deletion_id_unique ON deletion_ledger.marks (environment, deletion_id);
REVOKE ALL ON TABLE deletion_ledger.marks FROM PUBLIC;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'alliance_ledger_app') THEN
        CREATE ROLE alliance_ledger_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
    END IF;
END $$;
SELECT format('ALTER ROLE alliance_ledger_app PASSWORD %L', :'ledger_app_password') \gexec
REVOKE ALL ON TABLE deletion_ledger.marks FROM alliance_ledger_app;
GRANT USAGE ON SCHEMA deletion_ledger TO alliance_ledger_app;
GRANT SELECT, INSERT ON TABLE deletion_ledger.marks TO alliance_ledger_app;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO alliance_ledger_app;
ALTER ROLE alliance_ledger_app SET synchronous_commit = on;

-- The working application role also needs read access to pg_control_system() in its own cluster
-- so the application can reject a ledger accidentally configured in that same cluster.
