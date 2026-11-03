-- CANDIDATE ONLY. No production migration has been executed.
-- Mutable CRM control state, separate from the immutable audit ledger.
BEGIN;
CREATE SCHEMA IF NOT EXISTS crm_workflow_control;
CREATE TABLE IF NOT EXISTS crm_workflow_control.sales_hold (
 lead_id text NOT NULL, workflow text NOT NULL CHECK(workflow IN ('sms_rest','qa','education','recovery')),
 token text NOT NULL, acquired_at text NOT NULL, expires_at text, payload text NOT NULL,
 PRIMARY KEY(lead_id,workflow)
);
CREATE TABLE IF NOT EXISTS crm_workflow_control.sms_cycle (
 lead_id text PRIMARY KEY, token text UNIQUE NOT NULL, phase text NOT NULL, due_at text, payload text NOT NULL
);
CREATE INDEX IF NOT EXISTS sms_due ON crm_workflow_control.sms_cycle(due_at);
CREATE TABLE IF NOT EXISTS crm_workflow_control.workflow_event (
 event_id text PRIMARY KEY, received_at text NOT NULL, payload text NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_workflow_control.workflow_write (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, lead_id text NOT NULL,
 token text NOT NULL, state text NOT NULL, payload text NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_workflow_control.workflow_meta (key text PRIMARY KEY,value text NOT NULL);
INSERT INTO crm_workflow_control.workflow_meta VALUES
 ('tenant','88a28173-2a61-4cbb-92ea-f6b8be4592f3/orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC')
 ON CONFLICT DO NOTHING;
DO $$ BEGIN
 IF (SELECT value FROM crm_workflow_control.workflow_meta WHERE key='tenant') IS DISTINCT FROM
 '88a28173-2a61-4cbb-92ea-f6b8be4592f3/orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC' THEN
  RAISE EXCEPTION 'Existing workflow schema belongs to another tenant';
 END IF;
END $$;
INSERT INTO crm_workflow_control.workflow_meta VALUES ('hold_contract_version','1') ON CONFLICT DO NOTHING;
DO $$ BEGIN
 IF (SELECT value FROM crm_workflow_control.workflow_meta WHERE key='hold_contract_version') IS DISTINCT FROM '1' THEN
  RAISE EXCEPTION 'Existing workflow schema has an incompatible contract';
 END IF;
END $$;
-- Execute only through the existing authorized migration path. Current runtime
-- role cannot CREATE schemas. Do not create a new account or grant audit access.
GRANT USAGE ON SCHEMA crm_workflow_control TO st_gateway_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
 crm_workflow_control.sales_hold, crm_workflow_control.sms_cycle,
 crm_workflow_control.workflow_event, crm_workflow_control.workflow_write,
 crm_workflow_control.workflow_meta TO st_gateway_writer;
GRANT USAGE, SELECT ON SEQUENCE crm_workflow_control.workflow_write_id_seq TO st_gateway_writer;
COMMIT;
