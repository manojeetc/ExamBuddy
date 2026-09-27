-- ExamBuddy Supabase schema (DDL)
-- Replaces local Response/*.json, Response/*.csv, AnswerLog/*.xlsx, and
-- ReadList/read_list.json with normalized Postgres tables.
-- Single-user application: every row is owned by the fixed user "Panda1".
--
-- Apply with the Supabase SQL editor, or:
--   psql "$SUPABASE_DB_URL" -f supabase/schema.sql

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
create table if not exists users (
  username text primary key,
  display_name text not null default 'Panda1',
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Answer keys (replaces AnswerLog/<Family>.xlsx)
-- ---------------------------------------------------------------------------
create table if not exists answer_keys (
  id bigint generated always as identity primary key,
  user_id text not null default 'Panda1' references users(username),
  exam_family text not null,
  exam_year integer not null,
  exam_section text not null,
  question_number integer not null,
  answer text not null check (answer in ('A', 'B', 'C', 'D', 'E')),
  topic_section text,
  topic text,
  difficulty text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, exam_family, exam_year, exam_section, question_number)
);
create index if not exists idx_answer_keys_lookup
  on answer_keys (user_id, exam_family, exam_year, exam_section);

-- ---------------------------------------------------------------------------
-- Exam sessions (replaces Response/<session_id>.json top-level fields)
-- ---------------------------------------------------------------------------
create table if not exists exam_sessions (
  session_id text primary key,
  user_id text not null default 'Panda1' references users(username),
  category text not null,
  variant text,
  exam_family text,
  exam text,
  exam_label text not null,
  exam_year integer,
  exam_section text,
  question_count integer not null,
  selection_mode text not null default 'all',
  content_section text default '',
  topic text default '',
  mode text not null default 'untimed' check (mode in ('timed', 'untimed')),
  duration_minutes integer,
  active_elapsed_seconds numeric(10, 2) not null default 0,
  current_index integer not null default 0,
  status text not null default 'in_progress' check (status in ('in_progress', 'evaluated')),
  answer_key_available boolean not null default false,
  answer_file text,
  started_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_exam_sessions_user_status on exam_sessions (user_id, status);
create index if not exists idx_exam_sessions_updated_at on exam_sessions (updated_at desc);

-- ---------------------------------------------------------------------------
-- Question pool per session (replaces session["question_pool"])
-- ---------------------------------------------------------------------------
create table if not exists exam_session_questions (
  session_id text not null references exam_sessions (session_id) on delete cascade,
  question_number integer not null,
  category text not null,
  variant text,
  section text default 'Uncategorized',
  topic text default 'Uncategorized',
  was_previously_correct boolean not null default false,
  was_previously_wrong boolean not null default false,
  primary key (session_id, question_number)
);

-- ---------------------------------------------------------------------------
-- Per-question responses (replaces session["responses"][q])
-- ---------------------------------------------------------------------------
create table if not exists exam_responses (
  session_id text not null,
  question_number integer not null,
  answer text check (answer in ('A', 'B', 'C', 'D', 'E')),
  time_spent_seconds numeric(10, 2) not null default 0,
  review boolean not null default false,
  review_status text not null default 'not_flagged'
    check (review_status in ('not_flagged', 'needs_review', 'completed')),
  note text default '',
  question_started_at timestamptz,
  question_ended_at timestamptz,
  active_started_at timestamptz,
  primary key (session_id, question_number),
  foreign key (session_id, question_number)
    references exam_session_questions (session_id, question_number) on delete cascade
);
create index if not exists idx_exam_responses_review
  on exam_responses (review_status) where review_status <> 'not_flagged';

-- ---------------------------------------------------------------------------
-- Cumulative time segments per question (replaces response["time_segments"])
-- ---------------------------------------------------------------------------
create table if not exists exam_response_time_segments (
  id bigint generated always as identity primary key,
  session_id text not null,
  question_number integer not null,
  started_at timestamptz,
  ended_at timestamptz,
  seconds numeric(10, 2) not null default 0,
  foreign key (session_id, question_number)
    references exam_responses (session_id, question_number) on delete cascade
);
create index if not exists idx_time_segments_session_question
  on exam_response_time_segments (session_id, question_number);

-- ---------------------------------------------------------------------------
-- Evaluation summary (replaces session["evaluation"])
-- ---------------------------------------------------------------------------
create table if not exists exam_evaluations (
  session_id text primary key references exam_sessions (session_id) on delete cascade,
  score numeric(10, 2) not null,
  maximum numeric(10, 2) not null,
  correct integer not null default 0,
  wrong integer not null default 0,
  blank integer not null default 0,
  scoring_name text,
  answer_file text,
  evaluated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Per-question evaluation detail (replaces session["evaluation"]["details"])
-- ---------------------------------------------------------------------------
create table if not exists exam_evaluation_details (
  session_id text not null references exam_evaluations (session_id) on delete cascade,
  question_number integer not null,
  response text,
  correct_answer text,
  result text not null check (result in ('Correct', 'Incorrect', 'Blank')),
  points numeric(10, 2) not null default 0,
  time_spent_seconds numeric(10, 2) not null default 0,
  primary key (session_id, question_number)
);

-- ---------------------------------------------------------------------------
-- Read List (replaces ReadList/read_list.json)
-- ---------------------------------------------------------------------------
create table if not exists reading_list (
  id uuid primary key default gen_random_uuid(),
  user_id text not null default 'Panda1' references users(username),
  url text not null,
  title text not null,
  summary text default '',
  headings text[] not null default '{}',
  read boolean not null default false,
  reading_minutes text default '',
  feedback text default '',
  added_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, url)
);
create index if not exists idx_reading_list_user_read on reading_list (user_id, read);

-- ---------------------------------------------------------------------------
-- Reading log: one history row per save, so repeated reflections are kept
-- instead of being overwritten (distinct from the current state on the
-- reading_list row above).
-- ---------------------------------------------------------------------------
create table if not exists reading_log (
  id bigint generated always as identity primary key,
  article_id uuid not null references reading_list (id) on delete cascade,
  user_id text not null default 'Panda1' references users(username),
  logged_at timestamptz not null default now(),
  reading_minutes integer,
  marked_read boolean not null default false,
  feedback text default ''
);
create index if not exists idx_reading_log_article on reading_log (article_id, logged_at desc);

-- ---------------------------------------------------------------------------
-- Convenience view matching the legacy per-session CSV export columns.
-- ---------------------------------------------------------------------------
create or replace view v_session_export as
select
  s.session_id,
  s.exam_label as "Practice Name",
  s.exam_family as "Exam Family",
  s.exam as "Exam",
  s.content_section as "Section",
  s.topic as "Topic",
  s.mode as "Mode",
  r.question_number as "Question",
  r.answer as "Response",
  r.review_status as "Review",
  r.note as "Note",
  r.time_spent_seconds as "Time Spent (seconds)",
  d.correct_answer as "Correct Answer",
  d.result as "Result",
  d.points as "Points"
from exam_sessions s
join exam_responses r on r.session_id = s.session_id
left join exam_evaluation_details d
  on d.session_id = s.session_id and d.question_number = r.question_number
order by s.session_id, r.question_number;

-- ---------------------------------------------------------------------------
-- Keep updated_at columns current on write.
-- ---------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_exam_sessions_updated_at on exam_sessions;
create trigger trg_exam_sessions_updated_at
  before update on exam_sessions
  for each row execute function set_updated_at();

drop trigger if exists trg_answer_keys_updated_at on answer_keys;
create trigger trg_answer_keys_updated_at
  before update on answer_keys
  for each row execute function set_updated_at();

drop trigger if exists trg_reading_list_updated_at on reading_list;
create trigger trg_reading_list_updated_at
  before update on reading_list
  for each row execute function set_updated_at();
