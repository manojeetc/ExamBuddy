import unittest
from types import SimpleNamespace

from split_exam_pdf import ExtractionError, QuestionMarker, _select_sequence, parse_exam_name, validate_question_sequence


class SplitExamPdfTests(unittest.TestCase):
    def test_parse_exam_name(self):
        self.assertEqual(parse_exam_name("AMC10_2025_A").section, "A")
        self.assertEqual(parse_exam_name("AMC8_2023").section, None)

    def test_complete_sequence(self):
        markers = [QuestionMarker(number, 0, number * 20, 40) for number in range(1, 26)]
        self.assertEqual(_select_sequence(markers, 25), markers)
        validate_question_sequence(markers, 25)

    def test_decimal_does_not_become_marker(self):
        markers = [QuestionMarker(1, 0, 20, 40), QuestionMarker(2, 0, 80, 40)]
        with self.assertRaises(ExtractionError):
            _select_sequence(markers, 3)

    def test_missing_question_fails(self):
        markers = [QuestionMarker(number, 0, number * 20, 40) for number in range(1, 25)]
        with self.assertRaises(ExtractionError):
            _select_sequence(markers, 25)

    def test_duplicate_number_fails_validation(self):
        markers = [QuestionMarker(1, 0, 20, 40), QuestionMarker(1, 0, 40, 40)]
        with self.assertRaises(ExtractionError):
            validate_question_sequence(markers, 2)


if __name__ == "__main__":
    unittest.main()
