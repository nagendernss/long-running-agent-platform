-- Core entities
CREATE TABLE contact (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    role TEXT NOT NULL,              -- client | provider | staff
    phone TEXT,
    email TEXT,
    timezone TEXT DEFAULT 'America/New_York',
    business_hours JSONB,            -- {"start": "09:00", "end": "17:00"}
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE case_record (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_contact_id UUID REFERENCES contact(id),
    matter_type TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Workflow instances (the durable, resumable unit)
CREATE TABLE workflow_instance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_type TEXT NOT NULL,
    case_id UUID REFERENCES case_record(id),
    state TEXT NOT NULL,
    context JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',   -- active | blocked | paused | completed
    attempt_count INT NOT NULL DEFAULT 0,
    next_wake_at TIMESTAMPTZ,
    wake_reason TEXT,
    wake_token TEXT,                 -- fencing token: only the job carrying this token may execute the wake
    wake_job_id BIGINT,              -- procrastinate job id for the pending wake (cancelled on reschedule)
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_due_instances ON workflow_instance (next_wake_at) WHERE status = 'active';

-- Append-only audit trail
CREATE TABLE event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq BIGSERIAL NOT NULL,          -- monotonic tiebreaker: audit order stays exact
                                     -- even when many events share a timestamp
    instance_id UUID REFERENCES workflow_instance(id),
    type TEXT NOT NULL,              -- attempt_started, outcome_recorded, state_changed,
                                     -- fact_updated, escalated, manual_reschedule, completed, ...
    payload JSONB NOT NULL DEFAULT '{}',
    idempotency_key TEXT UNIQUE,     -- set on attempt_started; duplicate wake => skipped
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_event_instance ON event (instance_id, seq);

-- Versioned facts (the generic write-back store)
CREATE TABLE entity_fact_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,       -- 'contact', 'case_record', etc.
    entity_id UUID NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    confidence FLOAT NOT NULL,
    source_event_id UUID REFERENCES event(id),
    status TEXT NOT NULL DEFAULT 'applied',  -- applied | proposed | rejected
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_fact_current ON entity_fact_version (entity_type, entity_id, field, created_at DESC);

-- Human-in-the-loop (data model now, UI later)
CREATE TABLE review_task (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id UUID REFERENCES workflow_instance(id),
    reason TEXT NOT NULL,
    context_snapshot JSONB,
    suggested_options JSONB,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved
    resolution JSONB,
    resolved_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX idx_review_pending ON review_task (created_at) WHERE status = 'pending';
