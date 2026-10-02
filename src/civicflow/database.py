"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);
-- 展后供需对接（expo）
CREATE TABLE IF NOT EXISTS expo_demands (
    demand_id TEXT PRIMARY KEY,
    demand_identity TEXT NOT NULL UNIQUE,
    buyer_org TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expo_demand_versions (
    demand_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    category TEXT NOT NULL,
    quantity_min INTEGER NOT NULL,
    quantity_max INTEGER NOT NULL,
    delivery_regions_json TEXT NOT NULL,
    certifications_json TEXT NOT NULL,
    window_from TEXT NOT NULL,
    window_to TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(demand_id, version_no)
);
CREATE TABLE IF NOT EXISTS expo_products (
    product_id TEXT PRIMARY KEY,
    supplier_org TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expo_product_versions (
    product_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    category TEXT NOT NULL,
    qty_min INTEGER NOT NULL,
    qty_max INTEGER NOT NULL,
    delivery_regions_json TEXT NOT NULL,
    certifications_json TEXT NOT NULL,
    window_from TEXT NOT NULL,
    window_to TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(product_id, version_no)
);
CREATE TABLE IF NOT EXISTS expo_lead_registry (
    demand_identity TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(demand_identity, source, source_ref)
);
CREATE TABLE IF NOT EXISTS expo_lead_sources (
    demand_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    PRIMARY KEY(demand_id, source, source_ref)
);
CREATE TABLE IF NOT EXISTS expo_opportunities (
    opportunity_id TEXT PRIMARY KEY,
    demand_id TEXT NOT NULL,
    demand_version_no INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    product_version_no INTEGER NOT NULL,
    total_qty INTEGER NOT NULL,
    fulfilled_qty INTEGER NOT NULL,
    remaining_qty INTEGER NOT NULL,
    current_stage TEXT,
    stage_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    match_grants_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expo_pipeline_records (
    record_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    stage_no INTEGER NOT NULL,
    stage TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    receipt_key TEXT NOT NULL UNIQUE,
    payload_digest TEXT NOT NULL,
    due_at TEXT,
    job_id TEXT,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS expo_pipeline_opportunity ON expo_pipeline_records(opportunity_id, stage_no);
CREATE TABLE IF NOT EXISTS expo_meetings (
    meeting_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    venue_resource_id TEXT NOT NULL,
    personnel_json TEXT NOT NULL,
    venue_reservation_id TEXT NOT NULL,
    personnel_reservations_json TEXT NOT NULL,
    status TEXT NOT NULL,
    buyer_confirmed INTEGER NOT NULL DEFAULT 0,
    supplier_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expo_minutes (
    minutes_id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    minutes_no INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    scope_summary TEXT NOT NULL,
    status TEXT NOT NULL,
    buyer_confirmed INTEGER NOT NULL DEFAULT 0,
    supplier_confirmed INTEGER NOT NULL DEFAULT 0,
    proposed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(meeting_id, minutes_no)
);
CREATE TABLE IF NOT EXISTS expo_intake_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    intake_key TEXT NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT NOT NULL DEFAULT '',
    resolved_by TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
