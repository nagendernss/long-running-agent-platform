-- Workflow types as data. A *template* is code (app/workflows/templates/*); a
-- workflow type is a row naming one plus the spec a person filled in. Workflows
-- with real branching stay code-defined and never appear here.
CREATE TABLE workflow_type (
    name        TEXT PRIMARY KEY,
    template    TEXT NOT NULL,
    spec        JSONB NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The two workflows that used to be Python modules, reproduced exactly.
INSERT INTO workflow_type (name, template, spec, description) VALUES
(
    'client_checkin', 'outreach',
    '{"message": "Hi {contact_name}, this is a quick check-in from your legal team. How are you feeling, and is there anything new we should know about?",
      "channel": "sms",
      "recipient_key": "target_contact_id",
      "retry_count": 2, "retry_interval_days": 1, "retry_schedule": ["1d", "3d"],
      "response_deadline_days": 2,
      "contact_role": "client",
      "on_reply": "repeat", "repeat_every_days": 14,
      "escalate_keywords": ["worse", "getting worse", "pain", "hospital", "emergency", "surgery",
                            "depressed", "anxious", "suicidal", "cannot sleep", "another lawyer",
                            "another attorney", "fire you", "unhappy", "frustrated"],
      "escalate_reason": "client_flag",
      "flag_reply": "Thanks for letting us know - a member of your legal team will reach out to you directly."}'::jsonb,
    'Recurring check-in with a client, escalating anything concerning.'
),
(
    'contact_update', 'outreach',
    '{"message": "{message}",
      "channel": "sms",
      "recipient_key": "target_contact_id",
      "retry_count": 2, "retry_interval_days": 1, "retry_schedule": ["1d", "3d"],
      "response_deadline_days": 2,
      "awaiting_state": "awaiting_ack",
      "on_reply": "complete"}'::jsonb,
    'Send one update to one contact and make sure it landed.'
);
