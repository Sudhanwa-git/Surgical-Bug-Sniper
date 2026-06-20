"""
db.py — Surgical Bug Sniper · SQLite store
Replaces metrics.json + attempted_issues.json with a proper queryable database.
Tables: runs, patches, pr_outcomes
"""
import sqlite3
import threading
import math
from pathlib import Path
from datetime import datetime

DB_FILE = Path(__file__).parent / "sniper.db"
_lock   = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")   # safe concurrent reads + writes
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    """Create tables if they don't exist. Idempotent — safe to call on every startup."""
    with _lock:
        with _conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id         TEXT    NOT NULL UNIQUE,
                repo           TEXT,
                started_at     TEXT    NOT NULL,
                finished_at    TEXT,
                duration_sec   REAL,
                outcome        TEXT,          -- 'success' | 'no_fix' | 'error'
                bugs_scanned   INTEGER DEFAULT 0,
                bugs_attempted INTEGER DEFAULT 0,
                pr_url         TEXT
            );

            CREATE TABLE IF NOT EXISTS patches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT    NOT NULL,
                repo          TEXT    NOT NULL,
                issue_number  INTEGER NOT NULL,
                issue_title   TEXT,
                patch_file    TEXT,
                match_type    TEXT,           -- 'exact' | 'fuzzy-ws' | 'indent-agnostic'
                lines_before  INTEGER,
                lines_after   INTEGER,
                pr_url        TEXT,
                root_cause    TEXT,           -- extracted root cause sentence from LLM
                files_changed TEXT,           -- comma-separated relative paths
                fix_lines     INTEGER,        -- net lines changed (after - before)
                llm_raw_output TEXT,           -- full LLM response (for fine-tune export)
                attempted_at  TEXT    NOT NULL,
                UNIQUE(repo, issue_number)
            );

            CREATE TABLE IF NOT EXISTS pr_outcomes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                pr_url        TEXT    NOT NULL UNIQUE,
                repo          TEXT    NOT NULL,
                issue_number  INTEGER NOT NULL,
                pr_number     INTEGER,
                state         TEXT    DEFAULT 'open',   -- 'open' | 'merged' | 'closed'
                last_polled   TEXT,
                merged_at     TEXT
            );
            """)

        # Non-destructive migration for existing DBs that lack the new columns
        with _conn() as c:
            existing = {row[1] for row in c.execute("PRAGMA table_info(patches)")}
            for col, typedef in [
                ("root_cause",     "TEXT"),
                ("files_changed",  "TEXT"),
                ("fix_lines",      "INTEGER"),
                ("llm_raw_output", "TEXT"),
            ]:
                if col not in existing:
                    c.execute(f"ALTER TABLE patches ADD COLUMN {col} {typedef}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ── Run tracking ──────────────────────────────────────────────────────────────

def run_start(run_id: str):
    with _lock:
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO runs (run_id, started_at) VALUES (?,?)",
                (run_id, _now()))


def run_finish(run_id: str, **kw):
    """Update a run row with finish details.  Accepts any column kwargs."""
    if not kw:
        return
    kw.setdefault("finished_at", _now())
    cols = ", ".join(f"{k}=?" for k in kw)
    with _lock:
        with _conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE run_id=?",
                      [*kw.values(), run_id])


# ── Issue dedup (replaces attempted_issues.json) ──────────────────────────────

def already_attempted(repo: str, issue_num: int) -> bool:
    with _conn() as c:
        return bool(c.execute(
            "SELECT 1 FROM patches WHERE repo=? AND issue_number=? LIMIT 1",
            (repo, issue_num)).fetchone())


def mark_attempted(repo: str, issue_num: int,
                   title: str = "", run_id: str = ""):
    with _lock:
        with _conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO patches
                   (run_id, repo, issue_number, issue_title, attempted_at)
                   VALUES (?,?,?,?,?)""",
                (run_id, repo, issue_num, title, _now()))


def patch_done(repo: str, issue_num: int, **kw):
    """Fill in fix details after a successful patch (file, match_type, lines…)."""
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    with _lock:
        with _conn() as c:
            c.execute(
                f"UPDATE patches SET {cols} WHERE repo=? AND issue_number=?",
                [*kw.values(), repo, issue_num])


# ── PR outcome tracking ───────────────────────────────────────────────────────

def pr_add(pr_url: str, repo: str, issue_num: int, pr_num: int):
    with _lock:
        with _conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO pr_outcomes
                   (pr_url, repo, issue_number, pr_number, last_polled)
                   VALUES (?,?,?,?,?)""",
                (pr_url, repo, issue_num, pr_num, _now()))


def pr_open_list() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pr_outcomes WHERE state='open' ORDER BY id DESC")]


def pr_update(pr_url: str, state: str, merged_at: str = ""):
    with _lock:
        with _conn() as c:
            c.execute(
                """UPDATE pr_outcomes
                   SET state=?, merged_at=?, last_polled=?
                   WHERE pr_url=?""",
                (state, merged_at, _now(), pr_url))


# ── Learning: merge patterns ─────────────────────────────────────────────────

_MIN_OUTCOMES = 20   # minimum resolved PRs before patterns are injected


def get_merge_patterns(repo_full: str | None = None) -> str:
    """
    Query SQLite for merge-rate patterns (global + per-repo).
    Returns a compact one-block string ready for prompt injection, or "" if
    fewer than _MIN_OUTCOMES resolved (merged/closed) PRs exist in the DB.

    Pure SQL — no LLM call, executes in <2ms.
    """
    with _conn() as c:
        resolved = c.execute(
            "SELECT COUNT(*) FROM pr_outcomes WHERE state IN ('merged','closed')"
        ).fetchone()[0]

        if resolved < _MIN_OUTCOMES:
            return ""  # not enough data yet — stay silent

        # ── Global patterns: merge rate by root-cause category ────────────────
        rows = c.execute("""
            SELECT p.root_cause,
                   COUNT(*)                                         AS total,
                   SUM(CASE WHEN o.state='merged' THEN 1 ELSE 0 END) AS merged
            FROM patches p
            JOIN pr_outcomes o ON o.pr_url = p.pr_url
            WHERE p.root_cause IS NOT NULL AND p.root_cause != ''
              AND o.state IN ('merged', 'closed')
            GROUP BY p.root_cause
            HAVING total >= 2
            ORDER BY (merged * 1.0 / total) DESC
            LIMIT 5
        """).fetchall()

        global_lines = []
        for row in rows:
            rate = round(row["merged"] / row["total"] * 100)
            # Only surface patterns worth acting on (>40% merge rate)
            if rate >= 40:
                global_lines.append(f"{row['root_cause'][:60]} ({rate}% merged)")

        # ── Per-repo patterns (only if repo_full provided) ────────────────────
        repo_lines = []
        if repo_full:
            rrows = c.execute("""
                SELECT p.root_cause,
                       COUNT(*)                                         AS total,
                       SUM(CASE WHEN o.state='merged' THEN 1 ELSE 0 END) AS merged
                FROM patches p
                JOIN pr_outcomes o ON o.pr_url = p.pr_url
                WHERE p.repo = ?
                  AND p.root_cause IS NOT NULL AND p.root_cause != ''
                  AND o.state IN ('merged', 'closed')
                GROUP BY p.root_cause
                ORDER BY merged DESC
                LIMIT 3
            """, (repo_full,)).fetchall()

            # Also surface which file areas tend to merge for this repo
            file_rows = c.execute("""
                SELECT p.files_changed,
                       SUM(CASE WHEN o.state='merged' THEN 1 ELSE 0 END) AS merged
                FROM patches p
                JOIN pr_outcomes o ON o.pr_url = p.pr_url
                WHERE p.repo = ? AND p.files_changed IS NOT NULL
                  AND o.state IN ('merged', 'closed')
                GROUP BY p.files_changed
                ORDER BY merged DESC
                LIMIT 3
            """, (repo_full,)).fetchall()

            for rr in rrows:
                rate = round(rr["merged"] / rr["total"] * 100) if rr["total"] else 0
                if rate >= 40:
                    repo_lines.append(f"{rr['root_cause'][:60]} ({rate}% merged in this repo)")

            for fr in file_rows:
                if fr["merged"] > 0 and fr["files_changed"]:
                    repo_lines.append(f"File area '{fr['files_changed'][:40]}' has merged before")

    if not global_lines and not repo_lines:
        return ""

    parts = []
    if repo_lines:
        parts.append("Repo history: " + "; ".join(repo_lines[:4]))
    if global_lines:
        parts.append("Global history: " + "; ".join(global_lines[:3]))

    return "HISTORICAL CONTEXT (use to guide fix approach):\n" + "\n".join(parts)


# ── Stats for UI ──────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with _conn() as c:
        prs_opened  = c.execute("SELECT COUNT(*) FROM pr_outcomes").fetchone()[0]
        prs_merged  = c.execute("SELECT COUNT(*) FROM pr_outcomes WHERE state='merged'").fetchone()[0]
        prs_closed  = c.execute("SELECT COUNT(*) FROM pr_outcomes WHERE state='closed'").fetchone()[0]
        total_runs  = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        total_fixes = c.execute("SELECT COUNT(*) FROM patches WHERE patch_file IS NOT NULL").fetchone()[0]
        issues_scanned   = c.execute("SELECT COUNT(*) FROM patches").fetchone()[0]
        issues_attempted = c.execute("SELECT COUNT(*) FROM patches WHERE patch_file IS NOT NULL OR attempted_at IS NOT NULL").fetchone()[0]
        win_rate    = round(prs_merged / prs_opened * 100, 1) if prs_opened > 0 else 0.0
        recent_prs  = [dict(r) for r in c.execute(
            "SELECT * FROM pr_outcomes ORDER BY id DESC LIMIT 10")]
    return {
        "prs_opened":       prs_opened,
        "prs_merged":       prs_merged,
        "prs_closed":       prs_closed,
        "total_runs":       total_runs,
        "total_fixes":      total_fixes,
        "issues_scanned":   issues_scanned,
        "issues_attempted": issues_attempted,
        "win_rate":         win_rate,
        "recent_prs":       recent_prs,
    }


# ── Cosine similarity (no numpy needed) ──────────────────────────────────────

def _cosine(a: list[float], b: list[float],
            mag_a: float | None = None) -> float:
    """Cosine similarity.  Pass pre-computed mag_a to avoid re-calculating it
    when comparing a fixed query vector against many document vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot   = math.fsum(x * y for x, y in zip(a, b))
    mag_a = mag_a if mag_a is not None else math.sqrt(math.fsum(x * x for x in a))
    mag_b = math.sqrt(math.fsum(y * y for y in b))
    return dot / (mag_a * mag_b) if (mag_a and mag_b) else 0.0
