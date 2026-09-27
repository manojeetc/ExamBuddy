"""Storage backend abstraction for ExamBuddy.

Set STORAGE_BACKEND=csv or STORAGE_BACKEND=supabase in a local .env file
(see .env.example) to choose where exam sessions and the Read List are
persisted. The single application user is always "Panda1".
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # psycopg2-binary is only required for the supabase backend
    psycopg2 = None

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
DEFAULT_USER = "Panda1"


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists() and RESOURCE_DIR != BASE_DIR:
        env_path = RESOURCE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "csv").strip().lower()
if STORAGE_BACKEND not in {"csv", "supabase"}:
    STORAGE_BACKEND = "csv"
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "").strip()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_read_item_id() -> str:
    return str(uuid.uuid4())


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).split(".")[0])
    except (TypeError, ValueError):
        return None


class StorageConfigError(RuntimeError):
    """Raised for misconfiguration the user can fix (missing URL, missing driver)."""


def _connect():
    if psycopg2 is None:
        raise StorageConfigError(
            "psycopg2-binary is required for the Supabase storage backend. "
            "Install it with: pip install psycopg2-binary"
        )
    if not SUPABASE_DB_URL:
        raise StorageConfigError("SUPABASE_DB_URL is not set. Add it to .env to use the Supabase storage backend.")
    return psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)


@contextmanager
def get_cursor(commit: bool = False):
    # A fresh connection per call (rather than a long-lived pool) avoids
    # "server closed the connection unexpectedly" errors when a pooled
    # connection goes stale after the app sits idle for a while — Supabase's
    # pooler/Postgres closes idle backend connections after a timeout.
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """Create tables/views/triggers if missing and seed the single app user."""
    schema_sql = (RESOURCE_DIR / "supabase" / "schema.sql").read_text(encoding="utf-8")
    with get_cursor(commit=True) as cur:
        cur.execute(schema_sql)
        cur.execute(
            "insert into users (username, display_name) values (%s, %s) on conflict (username) do nothing",
            (DEFAULT_USER, DEFAULT_USER),
        )


# ---------------------------------------------------------------------------
# Exam sessions
# ---------------------------------------------------------------------------
def _group_by(rows: list[dict], key: str) -> dict[Any, list[dict]]:
    grouped: dict[Any, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def _build_session(
    session_row: dict,
    pool_rows: list[dict],
    response_rows: list[dict],
    segment_rows: list[dict],
    evaluation_row: dict | None,
    detail_rows: list[dict],
) -> dict[str, Any]:
    """Assemble a session dict from already-fetched rows (no DB access here)."""
    session_id = session_row["session_id"]
    session: dict[str, Any] = {
        "session_id": session_id,
        "category": session_row["category"],
        "variant": session_row["variant"],
        "exam_family": session_row["exam_family"],
        "exam": session_row["exam"],
        "exam_label": session_row["exam_label"],
        "year": session_row["exam_year"],
        "section": session_row["exam_section"],
        "question_count": session_row["question_count"],
        "selection_mode": session_row["selection_mode"],
        "content_section": session_row["content_section"] or "",
        "topic": session_row["topic"] or "",
        "mode": session_row["mode"],
        "duration_minutes": session_row["duration_minutes"],
        "active_elapsed_seconds": float(session_row["active_elapsed_seconds"] or 0),
        "current_index": session_row["current_index"],
        "status": session_row["status"],
        "answer_key_available": session_row["answer_key_available"],
        "answer_file": session_row["answer_file"],
        "started_at": session_row["started_at"].isoformat() if session_row["started_at"] else _now_iso(),
        "updated_at": session_row["updated_at"].isoformat() if session_row["updated_at"] else _now_iso(),
    }

    pool_rows = sorted(pool_rows, key=lambda row: row["question_number"])
    session["question_numbers"] = [row["question_number"] for row in pool_rows]
    session["question_pool"] = [
        {
            "number": row["question_number"],
            "category": row["category"],
            "variant": row["variant"],
            "section": row["section"],
            "topic": row["topic"],
            "right": row["was_previously_correct"],
            "wrong": row["was_previously_wrong"],
        }
        for row in pool_rows
    ]

    segments_by_question: dict[int, list[dict]] = {}
    for row in sorted(segment_rows, key=lambda row: row["id"]):
        segments_by_question.setdefault(row["question_number"], []).append(
            {
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
                "seconds": float(row["seconds"] or 0),
            }
        )

    responses: dict[str, Any] = {}
    for row in response_rows:
        q = row["question_number"]
        entry: dict[str, Any] = {
            "answer": row["answer"],
            "time_spent_seconds": float(row["time_spent_seconds"] or 0),
            "review": row["review"],
            "review_status": row["review_status"],
            "note": row["note"] or "",
            "time_segments": segments_by_question.get(q, []),
        }
        if row["question_started_at"]:
            entry["question_started_at"] = row["question_started_at"].isoformat()
        if row["question_ended_at"]:
            entry["question_ended_at"] = row["question_ended_at"].isoformat()
        if row["active_started_at"]:
            entry["active_started_at"] = row["active_started_at"].isoformat()
        responses[str(q)] = entry
    session["responses"] = responses

    if session_row["status"] == "evaluated" and evaluation_row:
        details = [
            {
                "question": row["question_number"],
                "response": row["response"],
                "correct_answer": row["correct_answer"],
                "result": row["result"],
                "points": float(row["points"] or 0),
                "time_spent_seconds": float(row["time_spent_seconds"] or 0),
            }
            for row in sorted(detail_rows, key=lambda row: row["question_number"])
        ]
        session["evaluation"] = {
            "score": float(evaluation_row["score"]),
            "maximum": float(evaluation_row["maximum"]),
            "correct": evaluation_row["correct"],
            "wrong": evaluation_row["wrong"],
            "blank": evaluation_row["blank"],
            "scoring_name": evaluation_row["scoring_name"],
            "answer_file": evaluation_row["answer_file"],
            "details": details,
            "evaluated_at": evaluation_row["evaluated_at"].isoformat() if evaluation_row["evaluated_at"] else _now_iso(),
        }
    return session


def fetch_session(session_id: str) -> dict[str, Any] | None:
    t0 = time.perf_counter()
    with get_cursor() as cur:
        cur.execute("select * from exam_sessions where session_id = %s and user_id = %s", (session_id, DEFAULT_USER))
        session_row = cur.fetchone()
        if not session_row:
            return None

        cur.execute(
            "select question_number, category, variant, section, topic, was_previously_correct, was_previously_wrong "
            "from exam_session_questions where session_id = %s",
            (session_id,),
        )
        pool_rows = cur.fetchall()

        cur.execute(
            "select question_number, answer, time_spent_seconds, review, review_status, note, "
            "question_started_at, question_ended_at, active_started_at "
            "from exam_responses where session_id = %s",
            (session_id,),
        )
        response_rows = cur.fetchall()

        cur.execute(
            "select id, question_number, started_at, ended_at, seconds "
            "from exam_response_time_segments where session_id = %s",
            (session_id,),
        )
        segment_rows = cur.fetchall()

        evaluation_row = None
        detail_rows: list[dict] = []
        if session_row["status"] == "evaluated":
            cur.execute("select * from exam_evaluations where session_id = %s", (session_id,))
            evaluation_row = cur.fetchone()
            if evaluation_row:
                cur.execute(
                    "select question_number, response, correct_answer, result, points, time_spent_seconds "
                    "from exam_evaluation_details where session_id = %s",
                    (session_id,),
                )
                detail_rows = cur.fetchall()

    result = _build_session(session_row, pool_rows, response_rows, segment_rows, evaluation_row, detail_rows)
    print(f"[ExamBuddy][storage] fetch_session({session_id}) took {(time.perf_counter() - t0) * 1000:.1f}ms (6 queries)")
    return result


def fetch_all_sessions() -> list[dict[str, Any]]:
    """Fetch every session with a fixed number of bulk queries instead of
    per-session round trips, so page load time doesn't scale with session
    count (previously 1 + 5N queries for N sessions)."""
    t0 = time.perf_counter()
    with get_cursor() as cur:
        cur.execute("select * from exam_sessions where user_id = %s", (DEFAULT_USER,))
        session_rows = cur.fetchall()
        session_ids = [row["session_id"] for row in session_rows]
        if not session_ids:
            print(f"[ExamBuddy][storage] fetch_all_sessions() found 0 sessions in {(time.perf_counter() - t0) * 1000:.1f}ms")
            return []

        cur.execute(
            "select session_id, question_number, category, variant, section, topic, "
            "was_previously_correct, was_previously_wrong "
            "from exam_session_questions where session_id = any(%s)",
            (session_ids,),
        )
        pool_by_session = _group_by(cur.fetchall(), "session_id")

        cur.execute(
            "select session_id, question_number, answer, time_spent_seconds, review, review_status, note, "
            "question_started_at, question_ended_at, active_started_at "
            "from exam_responses where session_id = any(%s)",
            (session_ids,),
        )
        responses_by_session = _group_by(cur.fetchall(), "session_id")

        cur.execute(
            "select session_id, id, question_number, started_at, ended_at, seconds "
            "from exam_response_time_segments where session_id = any(%s)",
            (session_ids,),
        )
        segments_by_session = _group_by(cur.fetchall(), "session_id")

        evaluated_ids = [row["session_id"] for row in session_rows if row["status"] == "evaluated"]
        evaluations_by_session: dict[str, dict] = {}
        details_by_session: dict[str, list[dict]] = {}
        if evaluated_ids:
            cur.execute("select * from exam_evaluations where session_id = any(%s)", (evaluated_ids,))
            evaluations_by_session = {row["session_id"]: row for row in cur.fetchall()}

            cur.execute(
                "select session_id, question_number, response, correct_answer, result, points, time_spent_seconds "
                "from exam_evaluation_details where session_id = any(%s)",
                (evaluated_ids,),
            )
            details_by_session = _group_by(cur.fetchall(), "session_id")

    sessions = [
        _build_session(
            row,
            pool_by_session.get(row["session_id"], []),
            responses_by_session.get(row["session_id"], []),
            segments_by_session.get(row["session_id"], []),
            evaluations_by_session.get(row["session_id"]),
            details_by_session.get(row["session_id"], []),
        )
        for row in session_rows
    ]
    print(f"[ExamBuddy][storage] fetch_all_sessions() fetched {len(sessions)} session(s) in {(time.perf_counter() - t0) * 1000:.1f}ms (6 bulk queries total)")
    return sessions


def save_session_record(session: dict[str, Any]) -> None:
    """Upsert a session using bulk statements instead of one round trip per
    question — autosave fires on every answer/navigation, and a 25-question
    exam was previously costing 50+ round trips per save."""
    t0 = time.perf_counter()
    session_id = session["session_id"]
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            insert into exam_sessions (
              session_id, user_id, category, variant, exam_family, exam, exam_label,
              exam_year, exam_section, question_count, selection_mode, content_section,
              topic, mode, duration_minutes, active_elapsed_seconds, current_index,
              status, answer_key_available, answer_file, started_at
            ) values (
              %(session_id)s, %(user_id)s, %(category)s, %(variant)s, %(exam_family)s, %(exam)s, %(exam_label)s,
              %(exam_year)s, %(exam_section)s, %(question_count)s, %(selection_mode)s, %(content_section)s,
              %(topic)s, %(mode)s, %(duration_minutes)s, %(active_elapsed_seconds)s, %(current_index)s,
              %(status)s, %(answer_key_available)s, %(answer_file)s, %(started_at)s
            )
            on conflict (session_id) do update set
              category = excluded.category, variant = excluded.variant, exam_family = excluded.exam_family,
              exam = excluded.exam, exam_label = excluded.exam_label, exam_year = excluded.exam_year,
              exam_section = excluded.exam_section, question_count = excluded.question_count,
              selection_mode = excluded.selection_mode, content_section = excluded.content_section,
              topic = excluded.topic, mode = excluded.mode, duration_minutes = excluded.duration_minutes,
              active_elapsed_seconds = excluded.active_elapsed_seconds, current_index = excluded.current_index,
              status = excluded.status, answer_key_available = excluded.answer_key_available,
              answer_file = excluded.answer_file
            """,
            {
                "session_id": session_id,
                "user_id": DEFAULT_USER,
                "category": session.get("category"),
                "variant": session.get("variant"),
                "exam_family": session.get("exam_family", session.get("category")),
                "exam": session.get("exam", session.get("variant")),
                "exam_label": session["exam_label"],
                "exam_year": _safe_int(session.get("year")),
                "exam_section": session.get("section"),
                "question_count": session["question_count"],
                "selection_mode": session.get("selection_mode", "all"),
                "content_section": session.get("content_section", ""),
                "topic": session.get("topic", ""),
                "mode": session.get("mode", "untimed"),
                "duration_minutes": session.get("duration_minutes"),
                "active_elapsed_seconds": session.get("active_elapsed_seconds", 0.0),
                "current_index": session.get("current_index", 0),
                "status": session.get("status", "in_progress"),
                "answer_key_available": bool(session.get("answer_key_available", False)),
                "answer_file": session.get("answer_file"),
                "started_at": session.get("started_at") or _now_iso(),
            },
        )

        default_numbers = range(1, int(session.get("question_count", 0)) + 1)
        pool = session.get("question_pool") or [
            {"number": number, "category": session.get("category"), "variant": session.get("variant")}
            for number in (session.get("question_numbers") or default_numbers)
        ]
        if pool:
            psycopg2.extras.execute_values(
                cur,
                """
                insert into exam_session_questions (
                  session_id, question_number, category, variant, section, topic,
                  was_previously_correct, was_previously_wrong
                ) values %s
                on conflict (session_id, question_number) do update set
                  category = excluded.category, variant = excluded.variant,
                  section = excluded.section, topic = excluded.topic,
                  was_previously_correct = excluded.was_previously_correct,
                  was_previously_wrong = excluded.was_previously_wrong
                """,
                [
                    (
                        session_id, item["number"], item.get("category", session.get("category")),
                        item.get("variant", session.get("variant")),
                        item.get("section", "Uncategorized"), item.get("topic", "Uncategorized"),
                        bool(item.get("right", False)), bool(item.get("wrong", False)),
                    )
                    for item in pool
                ],
            )

        responses = session.get("responses") or {}
        if responses:
            psycopg2.extras.execute_values(
                cur,
                """
                insert into exam_responses (
                  session_id, question_number, answer, time_spent_seconds, review, review_status,
                  note, question_started_at, question_ended_at, active_started_at
                ) values %s
                on conflict (session_id, question_number) do update set
                  answer = excluded.answer, time_spent_seconds = excluded.time_spent_seconds,
                  review = excluded.review, review_status = excluded.review_status, note = excluded.note,
                  question_started_at = excluded.question_started_at, question_ended_at = excluded.question_ended_at,
                  active_started_at = excluded.active_started_at
                """,
                [
                    (
                        session_id, int(q_str), response.get("answer"),
                        response.get("time_spent_seconds", 0.0),
                        bool(response.get("review", False)),
                        response.get("review_status") or ("needs_review" if response.get("review") else "not_flagged"),
                        response.get("note", ""),
                        response.get("question_started_at"),
                        response.get("question_ended_at"),
                        response.get("active_started_at"),
                    )
                    for q_str, response in responses.items()
                ],
            )

            cur.execute(
                "delete from exam_response_time_segments where session_id = %s and question_number = any(%s)",
                (session_id, [int(q_str) for q_str in responses]),
            )
            segment_values = [
                (session_id, int(q_str), segment.get("started_at"), segment.get("ended_at"), segment.get("seconds", 0.0))
                for q_str, response in responses.items()
                for segment in (response.get("time_segments") or [])
            ]
            if segment_values:
                psycopg2.extras.execute_values(
                    cur,
                    "insert into exam_response_time_segments (session_id, question_number, started_at, ended_at, seconds) values %s",
                    segment_values,
                )

        evaluation = session.get("evaluation")
        if evaluation:
            cur.execute(
                """
                insert into exam_evaluations (session_id, score, maximum, correct, wrong, blank, scoring_name, answer_file, evaluated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (session_id) do update set
                  score = excluded.score, maximum = excluded.maximum, correct = excluded.correct,
                  wrong = excluded.wrong, blank = excluded.blank, scoring_name = excluded.scoring_name,
                  answer_file = excluded.answer_file, evaluated_at = excluded.evaluated_at
                """,
                (
                    session_id, evaluation["score"], evaluation["maximum"], evaluation.get("correct", 0),
                    evaluation.get("wrong", 0), evaluation.get("blank", 0), evaluation.get("scoring_name"),
                    evaluation.get("answer_file"), evaluation.get("evaluated_at") or _now_iso(),
                ),
            )
            cur.execute("delete from exam_evaluation_details where session_id = %s", (session_id,))
            detail_values = [
                (
                    session_id, detail["question"], detail.get("response"), detail.get("correct_answer"),
                    detail["result"], detail.get("points", 0.0), detail.get("time_spent_seconds", 0.0),
                )
                for detail in evaluation.get("details", [])
            ]
            if detail_values:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    insert into exam_evaluation_details (session_id, question_number, response, correct_answer, result, points, time_spent_seconds)
                    values %s
                    """,
                    detail_values,
                )
    print(f"[ExamBuddy][storage] save_session_record({session_id}) took {(time.perf_counter() - t0) * 1000:.1f}ms")


def delete_session_record(session_id: str) -> None:
    with get_cursor(commit=True) as cur:
        cur.execute("delete from exam_sessions where session_id = %s and user_id = %s", (session_id, DEFAULT_USER))


# ---------------------------------------------------------------------------
# Read List
# ---------------------------------------------------------------------------
def fetch_read_items() -> list[dict[str, Any]]:
    with get_cursor() as cur:
        cur.execute(
            "select id, url, title, summary, headings, read, reading_minutes, feedback, added_at "
            "from reading_list where user_id = %s order by added_at desc",
            (DEFAULT_USER,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(row["id"]),
            "url": row["url"],
            "title": row["title"],
            "summary": row["summary"] or "",
            "headings": list(row["headings"] or []),
            "read": row["read"],
            "reading_minutes": row["reading_minutes"] or "",
            "feedback": row["feedback"] or "",
            "added_at": row["added_at"].isoformat() if row["added_at"] else _now_iso(),
        }
        for row in rows
    ]


def insert_read_item(item: dict[str, Any]) -> None:
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            insert into reading_list (id, user_id, url, title, summary, headings, read, reading_minutes, feedback, added_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (user_id, url) do nothing
            """,
            (
                item["id"], DEFAULT_USER, item["url"], item["title"], item.get("summary", ""),
                item.get("headings", []), item.get("read", False), item.get("reading_minutes", ""),
                item.get("feedback", ""), item.get("added_at") or _now_iso(),
            ),
        )


def update_read_item_record(item_id: str, read: bool, reading_minutes: str, feedback: str) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "update reading_list set read = %s, reading_minutes = %s, feedback = %s where id = %s and user_id = %s",
            (read, reading_minutes, feedback, item_id, DEFAULT_USER),
        )
        updated = cur.rowcount > 0
        if updated:
            minutes_value = _safe_int(reading_minutes) if reading_minutes else None
            cur.execute(
                """
                insert into reading_log (article_id, user_id, reading_minutes, marked_read, feedback)
                values (%s, %s, %s, %s, %s)
                """,
                (item_id, DEFAULT_USER, minutes_value, read, feedback),
            )
    return updated


def delete_read_item_record(item_id: str) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute("delete from reading_list where id = %s and user_id = %s", (item_id, DEFAULT_USER))
        return cur.rowcount > 0
