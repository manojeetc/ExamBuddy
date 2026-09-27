-- ExamBuddy Supabase sample data (DML)
-- Run supabase/schema.sql first. This script is idempotent (safe to re-run).
--
--   psql "$SUPABASE_DB_URL" -f supabase/seed.sql

-- ---------------------------------------------------------------------------
-- Single application user
-- ---------------------------------------------------------------------------
insert into users (username, display_name)
values ('Panda1', 'Panda1')
on conflict (username) do nothing;

-- ---------------------------------------------------------------------------
-- Sample answer key rows (replaces AnswerLog/AMC10.xlsx rows)
-- ---------------------------------------------------------------------------
insert into answer_keys (user_id, exam_family, exam_year, exam_section, question_number, answer, topic_section, topic, difficulty)
values
  ('Panda1', 'AMC10', 2025, 'A', 1, 'B', 'Algebra', 'Linear Equations', 'Easy'),
  ('Panda1', 'AMC10', 2025, 'A', 2, 'D', 'Geometry', 'Triangles', 'Medium'),
  ('Panda1', 'AMC10', 2025, 'A', 3, 'E', 'Number Theory', 'Divisibility', 'Hard')
on conflict (user_id, exam_family, exam_year, exam_section, question_number)
do update set
  answer = excluded.answer,
  topic_section = excluded.topic_section,
  topic = excluded.topic,
  difficulty = excluded.difficulty;

-- ---------------------------------------------------------------------------
-- Sample exam session
-- ---------------------------------------------------------------------------
insert into exam_sessions (
  session_id, user_id, category, variant, exam_family, exam, exam_label,
  exam_year, exam_section, question_count, selection_mode, content_section,
  topic, mode, duration_minutes, active_elapsed_seconds, current_index, status,
  answer_key_available, answer_file
) values (
  'AMC10_2025_A_sample_20260101_120000_abc123', 'Panda1', 'AMC10', 'AMC10_2025_A',
  'AMC10', 'AMC10_2025_A', 'AMC10 2025 A Practice', 2025, 'A', 3, 'all', '', '',
  'untimed', null, 145.5, 2, 'evaluated', true, 'AnswerLog/AMC10.xlsx'
)
on conflict (session_id) do nothing;

insert into exam_session_questions (session_id, question_number, category, variant, section, topic, was_previously_correct, was_previously_wrong)
values
  ('AMC10_2025_A_sample_20260101_120000_abc123', 1, 'AMC10', 'AMC10_2025_A', 'Algebra', 'Linear Equations', true, false),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 2, 'AMC10', 'AMC10_2025_A', 'Geometry', 'Triangles', false, true),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 3, 'AMC10', 'AMC10_2025_A', 'Number Theory', 'Divisibility', false, false)
on conflict (session_id, question_number) do nothing;

insert into exam_responses (session_id, question_number, answer, time_spent_seconds, review, review_status, note)
values
  ('AMC10_2025_A_sample_20260101_120000_abc123', 1, 'B', 62.5, false, 'not_flagged', ''),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 2, 'A', 91.0, true, 'needs_review', 'Mixed up the triangle inequality.'),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 3, null, 30.2, false, 'not_flagged', '')
on conflict (session_id, question_number) do nothing;

insert into exam_response_time_segments (session_id, question_number, started_at, ended_at, seconds)
values
  ('AMC10_2025_A_sample_20260101_120000_abc123', 1, now() - interval '20 minutes', now() - interval '19 minutes', 62.5),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 2, now() - interval '19 minutes', now() - interval '17 minutes 29 seconds', 91.0),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 3, now() - interval '17 minutes 29 seconds', now() - interval '17 minutes', 30.2);

insert into exam_evaluations (session_id, score, maximum, correct, wrong, blank, scoring_name, answer_file)
values (
  'AMC10_2025_A_sample_20260101_120000_abc123', 7.5, 18.0, 1, 1, 1, 'AMC scoring', 'AnswerLog/AMC10.xlsx'
)
on conflict (session_id) do nothing;

insert into exam_evaluation_details (session_id, question_number, response, correct_answer, result, points, time_spent_seconds)
values
  ('AMC10_2025_A_sample_20260101_120000_abc123', 1, 'B', 'B', 'Correct', 6.0, 62.5),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 2, 'A', 'D', 'Incorrect', 0.0, 91.0),
  ('AMC10_2025_A_sample_20260101_120000_abc123', 3, null, 'E', 'Blank', 1.5, 30.2)
on conflict (session_id, question_number) do nothing;

-- ---------------------------------------------------------------------------
-- Sample Read List article and its reading log history
-- ---------------------------------------------------------------------------
insert into reading_list (id, user_id, url, title, summary, headings, read, reading_minutes, feedback)
values (
  '11111111-1111-1111-1111-111111111111', 'Panda1',
  'https://example.com/articles/amc-problem-solving-strategies',
  'AMC Problem Solving Strategies',
  'A high-level overview of common strategies for tackling AMC-style problems.',
  array['Overview', 'Working Backwards', 'Estimation'],
  true, '18', 'Good refresher on estimation techniques before contest day.'
)
on conflict (user_id, url) do nothing;

insert into reading_log (article_id, user_id, reading_minutes, marked_read, feedback)
values
  ('11111111-1111-1111-1111-111111111111', 'Panda1', 10, false, 'First pass, skimmed the headings.'),
  ('11111111-1111-1111-1111-111111111111', 'Panda1', 18, true, 'Good refresher on estimation techniques before contest day.');
