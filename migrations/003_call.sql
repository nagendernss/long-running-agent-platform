-- A call is one attempt that happens to take minutes and involve a person talking.
-- It is placed inside the engine's transaction and then runs on its own; the
-- transcript is submitted back through handle_inbound exactly as a typed reply is.
CREATE TABLE call (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id UUID REFERENCES workflow_instance(id),
    contact_id  UUID REFERENCES contact(id),
    status      TEXT NOT NULL DEFAULT 'queued',    -- queued | ringing | active | completed | missed | failed
    goal        TEXT,                              -- what the agent is trying to achieve
    opening     TEXT,                              -- the first thing it says
    channel     TEXT NOT NULL DEFAULT 'call',
    to_address  TEXT,                              -- the number it "dialled"
    transcript  JSONB NOT NULL DEFAULT '[]',       -- [{who: agent|contact, text, at, source}]
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ
);

-- The phone page polls for something to ring.
CREATE INDEX idx_call_ringing ON call (created_at) WHERE status = 'ringing';
CREATE INDEX idx_call_instance ON call (instance_id, created_at DESC);
