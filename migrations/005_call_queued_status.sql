-- `ringing` was set the moment a call was placed, so it covered two different things:
-- a call waiting its turn that nobody can hear, and the one actually being offered.
-- The difference lived in whether ringing_since was null, which meant the status
-- column was lying for most of the rows it covered.
--
--   placed -> queued -> ringing -> active -> completed | missed | failed
UPDATE call SET status = 'queued' WHERE status = 'ringing' AND ringing_since IS NULL;

DROP INDEX IF EXISTS idx_call_ringing;
CREATE INDEX idx_call_waiting ON call (created_at) WHERE status IN ('queued', 'ringing');
