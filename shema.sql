-- ============================================================
-- COMPLIANCE SENTINEL — MySQL Schema
-- Run this once. Alembic migrations handle future changes.
-- ============================================================

CREATE DATABASE IF NOT EXISTS compliance
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE compliance;

CREATE TABLE rules (
    rule_id             VARCHAR(64)     NOT NULL,           -- e.g. GDPR-Art44-001
    rule_text           TEXT            NOT NULL,           -- raw regulatory text
    source_doc          VARCHAR(256)    NOT NULL,           -- e.g. GDPRv2.pdf
    article_ref         VARCHAR(128)        NULL,           -- e.g. Article 44
    version             INT             NOT NULL DEFAULT 1,
    status              ENUM(
                            'ACTIVE',
                            'DEPRECATED',
                            'DRAFT'
                        )               NOT NULL DEFAULT 'DRAFT',
    superseded_by       VARCHAR(64)         NULL,           -- FK to rules.rule_id
    obligation_type     ENUM(
                            'PROHIBITION',
                            'REQUIREMENT',
                            'PERMISSION'
                        )               NOT NULL,
    data_subject_scope  JSON                NULL,           -- ["EU_resident","minor",...]
    violation_conditions JSON           NOT NULL,           -- array of ViolationCondition objects
    effective_date      DATE            NOT NULL DEFAULT (CURRENT_DATE),
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (rule_id),
    CONSTRAINT fk_rules_superseded_by
        FOREIGN KEY (superseded_by)
        REFERENCES rules (rule_id)
        ON DELETE SET NULL,

    INDEX idx_rules_status          (status),
    INDEX idx_rules_source_doc      (source_doc),
    INDEX idx_rules_obligation_type (obligation_type),
    INDEX idx_rules_effective_date  (effective_date)
);


CREATE TABLE database_connections (
    id                      INT             NOT NULL AUTO_INCREMENT,
    name                    VARCHAR(256)    NOT NULL,           -- friendly name
    connection_string_enc   TEXT            NOT NULL,           -- AES-encrypted at rest
    db_type                 VARCHAR(64)     NOT NULL DEFAULT 'mysql',
    server_region           VARCHAR(64)         NULL,           -- e.g. us-east-1, eu-west-1
    scan_mode               ENUM(
                                'CDC',
                                'SCHEDULED',
                                'MANUAL'
                            )               NOT NULL DEFAULT 'SCHEDULED',
    cron_expression         VARCHAR(128)        NULL,           -- e.g. 0 2 * * *
    schema_map              JSON                NULL,           -- {table: {col: {category, sensitivity}}}
    schema_mapped           TINYINT(1)      NOT NULL DEFAULT 0, -- 0=pending, 1=complete
    owner_user_id           VARCHAR(128)        NULL,
    last_scanned_at         DATETIME            NULL,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                            ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    INDEX idx_dbconn_scan_mode      (scan_mode),
    INDEX idx_dbconn_schema_mapped  (schema_mapped),
    INDEX idx_dbconn_last_scanned   (last_scanned_at)
);


CREATE TABLE violations (
    id                      INT             NOT NULL AUTO_INCREMENT,
    db_connection_id        INT             NOT NULL,
    rule_id                 VARCHAR(64)     NOT NULL,
    table_name              VARCHAR(256)    NOT NULL,
    column_name             VARCHAR(256)        NULL,
    condition_matched       VARCHAR(512)    NOT NULL,   -- which violation_condition triggered
    evidence_snapshot       JSON                NULL,   -- anonymised sample values
    severity                ENUM(
                                'LOW',
                                'MEDIUM',
                                'HIGH',
                                'CRITICAL'
                            )               NOT NULL DEFAULT 'MEDIUM',
    status                  ENUM(
                                'OPEN',
                                'REMEDIATED',
                                'ACCEPTED_RISK',
                                'FALSE_POSITIVE'
                            )               NOT NULL DEFAULT 'OPEN',
    remediation_template    TEXT                NULL,
    detected_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at             DATETIME            NULL,
    resolved_by             VARCHAR(128)        NULL,

    PRIMARY KEY (id),
    CONSTRAINT fk_violations_db_connection
        FOREIGN KEY (db_connection_id)
        REFERENCES database_connections (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_violations_rule
        FOREIGN KEY (rule_id)
        REFERENCES rules (rule_id)
        ON DELETE RESTRICT,     -- never lose violation history if rule is deprecated

    INDEX idx_violations_status         (status),
    INDEX idx_violations_severity       (severity),
    INDEX idx_violations_rule_id        (rule_id),
    INDEX idx_violations_db_conn        (db_connection_id),
    INDEX idx_violations_detected_at    (detected_at),
    INDEX idx_violations_table_name     (table_name)
);


CREATE TABLE audit_logs (
    id                          INT             NOT NULL AUTO_INCREMENT,
    event_type                  VARCHAR(128)    NOT NULL,   -- VIOLATION_DETECTED | RULE_APPROVED | etc.
    entity_type                 VARCHAR(64)         NULL,   -- rule | violation | connection | workflow
    entity_id                   VARCHAR(128)        NULL,
    actor                       VARCHAR(128)        NULL,   -- user_id or "system"
    detail                      JSON                NULL,   -- arbitrary structured payload
    langgraph_checkpoint_id     VARCHAR(256)        NULL,   -- links event to graph run
    created_at                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    INDEX idx_audit_event_type      (event_type),
    INDEX idx_audit_entity          (entity_type, entity_id),
    INDEX idx_audit_actor           (actor),
    INDEX idx_audit_created_at      (created_at),
    INDEX idx_audit_checkpoint      (langgraph_checkpoint_id)
);

-- Immutability triggers (also installed programmatically in database.py)
CREATE TRIGGER prevent_audit_log_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
SIGNAL SQLSTATE '45000'
SET MESSAGE_TEXT = 'audit_logs is immutable: UPDATE not allowed';

CREATE TRIGGER prevent_audit_log_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
SIGNAL SQLSTATE '45000'
SET MESSAGE_TEXT = 'audit_logs is immutable: DELETE not allowed';


CREATE TABLE orchestrator_threads (
    thread_id           VARCHAR(128)    NOT NULL,           -- LangGraph thread_id (UUID)
    workflow_type       VARCHAR(64)     NOT NULL,           -- policy_review | remediation | conversational | ingestion
    status              ENUM(
                            'RUNNING',
                            'INTERRUPTED',                  -- waiting for human
                            'COMPLETED',
                            'FAILED',
                            'CANCELLED'
                        )               NOT NULL DEFAULT 'RUNNING',
    db_connection_id    INT                 NULL,           -- target DB if applicable
    user_message        TEXT                NULL,           -- original user query
    final_response      TEXT                NULL,           -- agent's final answer
    todos               JSON                NULL,           -- snapshot of todo list at completion
    pending_review      JSON                NULL,           -- HumanReviewRequest payload when INTERRUPTED
    human_decision      VARCHAR(64)         NULL,           -- approve | reject | modify | confirm_gap
    human_feedback      TEXT                NULL,
    actor               VARCHAR(128)        NULL,           -- who started this thread
    started_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    interrupted_at      DATETIME            NULL,           -- when HITL pause happened
    completed_at        DATETIME            NULL,
    error_detail        TEXT                NULL,

    PRIMARY KEY (thread_id),
    CONSTRAINT fk_threads_db_connection
        FOREIGN KEY (db_connection_id)
        REFERENCES database_connections (id)
        ON DELETE SET NULL,

    INDEX idx_threads_status        (status),
    INDEX idx_threads_workflow_type (workflow_type),
    INDEX idx_threads_actor         (actor),
    INDEX idx_threads_started_at    (started_at)
);


CREATE TABLE policy_review_findings (
    id                  INT             NOT NULL AUTO_INCREMENT,
    thread_id           VARCHAR(128)    NOT NULL,           -- which orchestrator run found this
    db_connection_id    INT             NOT NULL,
    rule_id             VARCHAR(64)     NOT NULL,
    finding_type        ENUM(
                            'COVERAGE_GAP',                 -- rule has no enforcement evidence
                            'PARTIAL_COVERAGE',             -- rule partially covered
                            'MISSING_SCHEMA_MAP',           -- schema not classified for this rule
                            'STALE_RULE'                    -- rule deprecated, no replacement
                        )               NOT NULL,
    description         TEXT                NULL,
    confirmed_by        VARCHAR(128)        NULL,           -- human who confirmed via HITL
    dismissed           TINYINT(1)      NOT NULL DEFAULT 0,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    CONSTRAINT fk_findings_thread
        FOREIGN KEY (thread_id)
        REFERENCES orchestrator_threads (thread_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_findings_db_connection
        FOREIGN KEY (db_connection_id)
        REFERENCES database_connections (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_findings_rule
        FOREIGN KEY (rule_id)
        REFERENCES rules (rule_id)
        ON DELETE RESTRICT,

    INDEX idx_findings_db_conn      (db_connection_id),
    INDEX idx_findings_rule_id      (rule_id),
    INDEX idx_findings_type         (finding_type),
    INDEX idx_findings_dismissed    (dismissed)
);


CREATE TABLE remediation_plans (
    id                  INT             NOT NULL AUTO_INCREMENT,
    thread_id           VARCHAR(128)    NOT NULL,
    violation_id        INT             NOT NULL,
    sql_statements      JSON            NOT NULL,           -- ["ALTER TABLE...", "UPDATE..."]
    risk_level          ENUM(
                            'LOW',
                            'MEDIUM',
                            'HIGH'
                        )               NOT NULL DEFAULT 'MEDIUM',
    rollback_plan       JSON                NULL,           -- reverse SQL statements
    estimated_impact    TEXT                NULL,
    status              ENUM(
                            'PROPOSED',
                            'APPROVED',
                            'REJECTED',
                            'EXECUTED',
                            'VERIFIED',
                            'FAILED'
                        )               NOT NULL DEFAULT 'PROPOSED',
    approved_by         VARCHAR(128)        NULL,
    executed_at         DATETIME            NULL,
    execution_report    JSON                NULL,           -- per-statement results
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    CONSTRAINT fk_remediation_thread
        FOREIGN KEY (thread_id)
        REFERENCES orchestrator_threads (thread_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_remediation_violation
        FOREIGN KEY (violation_id)
        REFERENCES violations (id)
        ON DELETE RESTRICT,

    INDEX idx_remediation_violation (violation_id),
    INDEX idx_remediation_status    (status),
    INDEX idx_remediation_thread    (thread_id)
);


CREATE TABLE pdf_ingestion_jobs (
    id                  INT             NOT NULL AUTO_INCREMENT,
    job_id              VARCHAR(128)    NOT NULL UNIQUE,    -- UUID returned to frontend
    thread_id           VARCHAR(128)        NULL,           -- if run through orchestrator
    filename            VARCHAR(256)    NOT NULL,
    source_doc          VARCHAR(256)    NOT NULL,           -- canonical name used in rules.source_doc
    status              ENUM(
                            'QUEUED',
                            'EXTRACTING',                   -- pass-1 in progress
                            'DECOMPOSING',                  -- pass-2 in progress
                            'AWAITING_REVIEW',              -- HITL: rules pending human approval
                            'COMPLETED',
                            'FAILED'
                        )               NOT NULL DEFAULT 'QUEUED',
    total_chunks        INT                 NULL,
    candidate_spans     INT                 NULL,           -- pass-1 output count
    rules_decomposed    INT                 NULL,           -- pass-2 output count
    rules_approved      INT             NOT NULL DEFAULT 0,
    rules_rejected      INT             NOT NULL DEFAULT 0,
    error_detail        TEXT                NULL,
    started_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at        DATETIME            NULL,

    PRIMARY KEY (id),
    CONSTRAINT fk_ingestion_thread
        FOREIGN KEY (thread_id)
        REFERENCES orchestrator_threads (thread_id)
        ON DELETE SET NULL,

    INDEX idx_ingestion_job_id      (job_id),
    INDEX idx_ingestion_status      (status),
    INDEX idx_ingestion_source_doc  (source_doc)
);

