-- Proof Bench bundle store (SITE-5). Minimal by design: bundles are
-- immutable once accepted; the rollup lives in KV and is rebuilt by CI.
CREATE TABLE IF NOT EXISTS bundles (
  bundle_id   TEXT PRIMARY KEY,
  received_at TEXT NOT NULL,
  payload     TEXT NOT NULL            -- the exact submitted JSON
);
