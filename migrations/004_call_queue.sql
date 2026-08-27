-- Calls are answered one at a time by one person, so they queue rather than all
-- ringing at once. `ringing_since` is stamped when a call reaches the front and is
-- actually being offered - the ring timeout counts from then, not from when it was
-- placed, so a call waiting behind a long conversation is not timed out for it.
ALTER TABLE call ADD COLUMN ringing_since TIMESTAMPTZ;
