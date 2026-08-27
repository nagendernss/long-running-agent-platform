-- Ordering a queue by created_at alone breaks whenever two calls share an instant -
-- several instances waking in the same tick, or a virtual clock that is not moving.
-- A monotonic sequence gives insertion order from the database, the same way event.seq
-- keeps the audit trail exact.
ALTER TABLE call ADD COLUMN seq BIGSERIAL;
DROP INDEX IF EXISTS idx_call_waiting;
CREATE INDEX idx_call_waiting ON call (created_at, seq) WHERE status IN ('queued', 'ringing');
