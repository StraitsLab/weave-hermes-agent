TRANSCRIPT_SEQ_KEY = "transcript_seq"


def _transcript_eligible_sql(alias: str) -> str:
    """Child that resume resolution may follow; mirrors get_compression_tip's
    exclusions (deliberately looser than resolve_resume_session_id's)."""
    mc = f"CASE WHEN json_valid({alias}.model_config) THEN {alias}.model_config ELSE '{{}}' END"
    return (
        f"(json_extract({mc}, '$._branched_from') IS NULL "
        f"AND json_extract({mc}, '$._delegate_from') IS NULL "
        f"AND COALESCE({alias}.source, '') != 'tool')"
    )


def _transcript_bump_sql(targets: str) -> str:
    # One database-wide high-water mark: the next epoch is above both the
    # stored sequence and EVERY epoch any session holds (one index seek on
    # idx_sessions_transcript_epoch). A copied or rewound state_meta (session
    # recovery copies sessions first, then state_meta) can therefore never
    # reissue an epoch — not for the target, nor for any lineage whose public
    # epoch is a MAX over several sessions.
    high = "(SELECT COALESCE(MAX(transcript_epoch), 0) FROM sessions)"
    return (
        f"INSERT INTO state_meta(key, value) VALUES ('transcript_seq', CAST({high} + 1 AS TEXT)) "
        f"ON CONFLICT(key) DO UPDATE SET value = CAST(MAX(CAST(value AS INTEGER), {high}) + 1 AS TEXT); "
        "UPDATE sessions SET transcript_epoch = "
        "(SELECT CAST(value AS INTEGER) FROM state_meta WHERE key = 'transcript_seq') "
        f"WHERE id IN ({targets});"
    )


# Columns /api/sessions/{id}/messages can render, plus the ones that decide
# membership. api_content, display_metadata (reactions), platform_message_id
# and observed are deliberately absent. _compressed_summary is model-only;
# the compaction transaction explicitly advances its cursor, including on empty
# history. Do not bind triggers to this optional migration column.
_TRANSCRIPT_MESSAGE_COLUMNS = (
    "id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name",
    "timestamp", "token_count", "finish_reason", "reasoning", "reasoning_content",
    "display_kind", "active", "compacted",
)
_TRANSCRIPT_SESSION_COLUMNS = (
    "parent_session_id", "ended_at", "end_reason", "model_config", "source", "started_at",
)


def _changed_sql(columns) -> str:
    return " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)


# Every session row insert takes a fresh epoch from the global sequence, so no
# incarnation of a session id (INSERT OR REPLACE, delete + recreate) reissues an
# epoch an older incarnation's cursors carry; sessions.id and messages.id are
# immutable; new message ids are positive.
TRANSCRIPT_TRIGGERS = (
    "transcript_epoch_message_id_immutable",
    "transcript_epoch_message_positive_id",
    "transcript_epoch_session_id_immutable",
    "transcript_epoch_session_reinsert",
    "transcript_epoch_message_replace",
    "transcript_epoch_message_insert_below_head",
    "transcript_epoch_message_update",
    "transcript_epoch_message_delete",
    "transcript_epoch_session_insert",
    "transcript_epoch_session_delete",
    "transcript_epoch_session_update",
)
TRANSCRIPT_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS transcript_epoch_message_id_immutable BEFORE UPDATE ON messages
WHEN OLD.id IS NOT NEW.id
BEGIN SELECT RAISE(ABORT, 'messages.id is immutable'); END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_message_positive_id AFTER INSERT ON messages
WHEN NEW.id <= 0
BEGIN SELECT RAISE(ABORT, 'messages.id must be positive'); END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_message_replace BEFORE INSERT ON messages
WHEN NEW.id > 0 AND EXISTS (SELECT 1 FROM messages _m WHERE _m.id = NEW.id)
BEGIN {_transcript_bump_sql("(SELECT _m.session_id FROM messages _m WHERE _m.id = NEW.id), NEW.session_id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_message_insert_below_head AFTER INSERT ON messages
WHEN EXISTS (SELECT 1 FROM messages _m WHERE _m.id > NEW.id)
BEGIN {_transcript_bump_sql("NEW.session_id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_message_update
AFTER UPDATE OF {", ".join(_TRANSCRIPT_MESSAGE_COLUMNS)} ON messages
WHEN {_changed_sql(_TRANSCRIPT_MESSAGE_COLUMNS)}
BEGIN {_transcript_bump_sql("OLD.session_id, NEW.session_id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_message_delete AFTER DELETE ON messages
BEGIN {_transcript_bump_sql("OLD.session_id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_session_insert AFTER INSERT ON sessions
WHEN NEW.parent_session_id IS NOT NULL AND {_transcript_eligible_sql('NEW')}
BEGIN {_transcript_bump_sql("NEW.parent_session_id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_session_id_immutable BEFORE UPDATE ON sessions
WHEN OLD.id IS NOT NEW.id
BEGIN SELECT RAISE(ABORT, 'sessions.id is immutable'); END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_session_reinsert AFTER INSERT ON sessions
BEGIN {_transcript_bump_sql("NEW.id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_session_delete AFTER DELETE ON sessions
WHEN OLD.parent_session_id IS NOT NULL AND {_transcript_eligible_sql('OLD')}
BEGIN {_transcript_bump_sql("OLD.parent_session_id")} END;
CREATE TRIGGER IF NOT EXISTS transcript_epoch_session_update
AFTER UPDATE OF {", ".join(_TRANSCRIPT_SESSION_COLUMNS)} ON sessions
WHEN ({_changed_sql(_TRANSCRIPT_SESSION_COLUMNS)})
 AND ({_transcript_eligible_sql('OLD')} OR {_transcript_eligible_sql('NEW')})
BEGIN {_transcript_bump_sql("NEW.id, OLD.parent_session_id, NEW.parent_session_id")} END;
"""

# Resume-eligible descendant tree of one session; its MAX(transcript_epoch) is
# that session's current epoch. UNION (not UNION ALL) makes cycles terminate.
TRANSCRIPT_EPOCH_SQL = f"""
WITH RECURSIVE _tree(id) AS (
    SELECT ?
    UNION
    SELECT _c.id FROM sessions _c JOIN _tree ON _c.parent_session_id = _tree.id
    WHERE {_transcript_eligible_sql('_c')}
)
SELECT COALESCE(MAX(_s.transcript_epoch), 0) FROM sessions _s JOIN _tree ON _s.id = _tree.id
"""


def transcript_trigger_statements() -> dict:
    """``{trigger name: CREATE TRIGGER statement}`` for TRANSCRIPT_TRIGGER_SQL."""
    import re as _re
    out = {}
    for statement in _re.split(r";\s*\n(?=CREATE TRIGGER)", TRANSCRIPT_TRIGGER_SQL.strip()):
        statement = statement.strip().rstrip(";") + ";" if not statement.strip().endswith("END;") else statement.strip()
        name = _re.match(r"CREATE TRIGGER IF NOT EXISTS (\w+)", statement).group(1)
        out[name] = statement.removesuffix(";")
    return out


def install(db):
    """Idempotent upgrade, including databases created by the v0.21 fork."""
    conn = db._conn
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "transcript_epoch" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN transcript_epoch INTEGER NOT NULL DEFAULT 0")
    wanted = transcript_trigger_statements()
    stored = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'transcript_epoch_%'"))
    normalize = lambda sql: " ".join((sql or "").replace("IF NOT EXISTS ", "").split())
    changed = [name for name, sql in wanted.items() if normalize(stored.get(name)) != normalize(sql)]
    stale = [name for name in stored if name not in wanted]
    if changed or stale:
        script = "".join(f"DROP TRIGGER IF EXISTS {name};" for name in changed + stale)
        script += "".join(wanted[name] + ";" for name in changed)
        conn.executescript("BEGIN IMMEDIATE;" + script + "COMMIT;")
