-- Newest-applied-wins decides which value the platform actually uses, and it was
-- decided by created_at alone. Two facts written in the same instant - one call
-- yielding a phone and an email, a reviewer approving one in the same second the
-- brain applies another - tie, and the tie was broken by whatever order the rows
-- came back in. The same fix event.seq and call.seq already carry: insertion order
-- from the database.
ALTER TABLE entity_fact_version ADD COLUMN seq BIGSERIAL;
DROP INDEX IF EXISTS idx_fact_current;
CREATE INDEX idx_fact_current ON entity_fact_version (entity_type, entity_id, field, created_at DESC, seq DESC);
