-- FreeClaw telemetry store.
--
-- install_id is the primary key so INSERT OR IGNORE dedupes retries for free.
-- There is deliberately no ip, user_agent, or hostname column: the schema is
-- the enforcement mechanism for what the README promises is collected.

CREATE TABLE IF NOT EXISTS installs (
    install_id     TEXT PRIMARY KEY,
    version        TEXT NOT NULL,
    os             TEXT NOT NULL,
    install_method TEXT NOT NULL,
    first_seen     TEXT NOT NULL
);

-- Backs the "installs per day" panel in /telemetry/stats.
CREATE INDEX IF NOT EXISTS idx_installs_first_seen ON installs (first_seen);
