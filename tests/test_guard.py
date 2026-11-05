import unittest

from usage_collector import guard

FREE_TEXT = "Fix the login bug in /home/example-user/app please"


def valid_row(**overrides):
    row = {
        "ts": "2026-01-02T03:04:05Z",
        "account": "work",
        "machine": "0123456789ab",
        "project": "abcdef012345",
        "session": "fedcba987654",
        "agent": "",
        "is_subagent": "0",
        "agent_type": "",
        "spawn_depth": "",
        "is_fork": "0",
        "model": "claude-opus-5-5",
        "effort": "high",
        "input": "10",
        "cache_read": "200",
        "cache_write": "30",
        "output": "40",
        "context": "240",
        "web_search": "0",
        "web_fetch": "1",
        "tools": "Bash;Read",
        "compacted": "0",
    }
    row.update(overrides)
    return row


class ValidateRowsTest(unittest.TestCase):
    def test_valid_rows_have_no_problems(self):
        subagent = valid_row(
            agent="aaaaaaaaaaaa", is_subagent="1",
            agent_type="plugin-name:agent-name", spawn_depth="2",
            is_fork="1", effort="", tools="", compacted="1",
            ts="2026-01-02T03:00:00Z",
        )
        self.assertEqual(guard.validate_rows([valid_row(), subagent]), [])

    def test_free_text_rejected_in_every_column(self):
        for column in guard.COLUMNS:
            with self.subTest(column=column):
                problems = guard.validate_rows([valid_row(**{column: FREE_TEXT})])
                self.assertTrue(problems)
                self.assertIn(column, problems[0])

    def test_problems_never_contain_the_offending_value(self):
        rows = [valid_row(**{column: FREE_TEXT}) for column in guard.COLUMNS]
        rows.append(dict(valid_row(), **{FREE_TEXT: "1"}))
        problems = guard.validate_rows(rows)
        self.assertGreaterEqual(len(problems), len(guard.COLUMNS) + 1)
        for problem in problems:
            self.assertNotIn("login", problem)
            self.assertNotIn("example-user", problem)

    def test_problem_names_row_index(self):
        problems = guard.validate_rows([valid_row(), valid_row(model="x y")])
        self.assertEqual(problems, ["row 1: column model is invalid"])

    def test_unknown_column_is_a_problem(self):
        row = dict(valid_row(), extra="1")
        self.assertEqual(guard.validate_rows([row]), ["row 0: unknown column"])

    def test_missing_column_is_a_problem(self):
        row = valid_row()
        del row["model"]
        self.assertEqual(
            guard.validate_rows([row]), ["row 0: column model is missing"]
        )

    def test_non_string_values_rejected(self):
        self.assertTrue(guard.validate_rows([valid_row(input=10)]))

    def test_rejects_specific_bad_values(self):
        cases = {
            "ts": ["2026-01-02 03:04:05", "2026-13-02T03:04:05Z", ""],
            "account": ["", "-work", "a" * 65, "work space"],
            "machine": [""],
            "project": ["ABCDEF012345", "abcdef01234", ""],
            "session": ["ABCDEF012345", "abcdef01234", ""],
            "agent": ["abc", "ABCDEF012345"],
            "is_subagent": ["2", ""],
            "is_fork": ["2", ""],
            "model": ["a b", "", "-claude"],
            "agent_type": ["has space"],
            "spawn_depth": ["-1", "1.5", "1000000001"],
            "effort": ["extreme"],
            "input": ["-1", "1000000001", "01x", ""],
            "cache_read": ["1000000001", "-1", ""],
            "cache_write": ["1000000001", "-1", ""],
            "output": ["1000000001", "-1", ""],
            "context": ["1000000001", "-1", ""],
            "web_search": ["1000000001", "-1", ""],
            "web_fetch": ["1000000001", "-1", ""],
            "tools": ["Bash;", ";Bash", "Bash;bad tool"],
            "compacted": ["yes"],
        }
        for column, values in cases.items():
            for value in values:
                with self.subTest(column=column, value=value):
                    problems = guard.validate_rows([valid_row(**{column: value})])
                    self.assertIn("row 0: column %s is invalid" % column, problems)

    def test_context_must_equal_sum_of_inputs(self):
        problems = guard.validate_rows([valid_row(context="1")])
        self.assertEqual(problems, ["row 0: column context is invalid"])

    def test_main_thread_row_cannot_carry_an_agent(self):
        problems = guard.validate_rows([valid_row(agent="aaaaaaaaaaaa")])
        self.assertEqual(problems, ["row 0: column agent is invalid"])


class ValidateCheckTotalsTest(unittest.TestCase):
    def totals(self):
        return {
            "2026-01-02": {
                "claude-opus-5-5": {
                    "input": 1, "output": 2, "cache_read": 3, "cache_write": 4,
                }
            }
        }

    def test_valid_totals(self):
        self.assertEqual(guard.validate_check_totals(self.totals()), [])

    def test_rejects_free_text_model_without_echoing_it(self):
        totals = {"2026-01-02": {FREE_TEXT: self.totals()["2026-01-02"]["claude-opus-5-5"]}}
        problems = guard.validate_check_totals(totals)
        self.assertTrue(problems)
        self.assertFalse(any("login" in p for p in problems))

    def test_rejects_bad_date_extra_keys_and_bad_numbers(self):
        bad_date = {"Jan 2": self.totals()["2026-01-02"]}
        extra_key = self.totals()
        extra_key["2026-01-02"]["claude-opus-5-5"]["note"] = 1
        bad_number = self.totals()
        bad_number["2026-01-02"]["claude-opus-5-5"]["input"] = "1"
        for totals in (bad_date, extra_key, bad_number, [], {"2026-01-02": []}):
            with self.subTest(totals=totals):
                self.assertTrue(guard.validate_check_totals(totals))


if __name__ == "__main__":
    unittest.main()
