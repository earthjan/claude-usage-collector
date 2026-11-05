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


def context_row(**overrides):
    row = {
        "ts": "2026-01-02T03:04:05Z", "account": "work",
        "machine": "0123456789ab", "project": "abcdef012345",
        "session": "fedcba987654", "agent": "", "is_subagent": "0",
        "agent_type": "", "event": "start",
        "system_prompt_chars": "1500", "instr_user_chars": "100",
        "instr_project_chars": "200", "instr_local_chars": "30",
        "instr_memory_chars": "40", "instr_other_chars": "50",
        "instr_files": "5", "skill_listing_chars": "300", "skill_count": "3",
        "agent_listing_chars": "100", "agent_count": "2",
        "deferred_tools_chars": "179", "deferred_tool_count": "3",
        "mcp_chars": "250", "mcp_server_count": "1",
        "hook_context_chars": "100", "first_turn_context": "1210",
    }
    row.update(overrides)
    return row


def skill_row(**overrides):
    row = {
        "ts": "2026-01-02T03:04:05Z", "account": "work",
        "machine": "0123456789ab", "project": "abcdef012345",
        "session": "fedcba987654", "agent": "", "skill": "plugin-x:review",
    }
    row.update(overrides)
    return row


def catalog_row(**overrides):
    row = {"date": "2026-01-02", "account": "work", "machine": "0123456789ab",
           "kind": "skill", "name": "plugin-x:review"}
    row.update(overrides)
    return row


STREAM_ROWS = {"context": context_row, "skills": skill_row, "catalog": catalog_row}


class ValidateStreamTest(unittest.TestCase):
    def test_valid_rows_of_every_stream_have_no_problems(self):
        rows = {
            "usage": [valid_row()],
            "context": [context_row(), context_row(
                agent="aaaaaaaaaaaa", is_subagent="1", agent_type="Explore",
                event="compact")],
            "skills": [skill_row(), skill_row(agent="aaaaaaaaaaaa")],
            "catalog": [catalog_row(), catalog_row(kind="agent", name="Explore")],
        }
        for stream, stream_rows in rows.items():
            with self.subTest(stream=stream):
                self.assertEqual(guard.validate_stream(stream, stream_rows), [])

    def test_free_text_rejected_in_every_column_without_echoing_it(self):
        for stream, make in STREAM_ROWS.items():
            for column in guard.SCHEMAS[stream].columns:
                with self.subTest(stream=stream, column=column):
                    problems = guard.validate_stream(stream, [make(**{column: FREE_TEXT})])
                    self.assertIn(
                        "%s row 0: column %s is invalid" % (stream, column), problems)
                    self.assertNotIn("login", " ".join(problems))

    def test_unknown_and_missing_columns_are_problems(self):
        extra = dict(context_row(), model="claude-opus-5-5")
        missing = skill_row()
        del missing["skill"]
        self.assertEqual(guard.validate_stream("context", [extra]),
                         ["context row 0: unknown column"])
        self.assertEqual(guard.validate_stream("skills", [missing]),
                         ["skills row 0: column skill is missing"])

    def test_rejects_specific_bad_values(self):
        cases = [
            ("context", "event", "resume"),
            ("context", "instr_files", "-1"),
            ("context", "first_turn_context", "1000000001"),
            ("context", "agent_type", "has space"),
            ("skills", "skill", ""),
            ("skills", "agent", "abc"),
            ("catalog", "kind", "tool"),
            ("catalog", "date", "2026-02-30"),
            ("catalog", "name", ""),
        ]
        for stream, column, value in cases:
            with self.subTest(stream=stream, column=column, value=value):
                problems = guard.validate_stream(
                    stream, [STREAM_ROWS[stream](**{column: value})])
                self.assertIn("%s row 0: column %s is invalid" % (stream, column), problems)

    def test_main_thread_context_row_cannot_carry_an_agent(self):
        problems = guard.validate_stream("context", [context_row(agent="aaaaaaaaaaaa")])
        self.assertEqual(problems, ["context row 0: column agent is invalid"])

    def test_usage_problems_keep_their_old_wording(self):
        problems = guard.validate_stream("usage", [valid_row(model="x y")])
        self.assertEqual(problems, ["row 0: column model is invalid"])

    def test_validate_streams_covers_every_batch(self):
        problems = guard.validate_streams({
            "usage": [valid_row()], "catalog": [catalog_row(kind="tool")],
        })
        self.assertEqual(problems, ["catalog row 0: column kind is invalid"])


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


class HashedNamesTest(unittest.TestCase):
    HASH = "0123456789ab"

    def problems(self, stream, row):
        return guard.validate_stream(stream, [row], hashed_names=True)

    def test_hashed_names_pass(self):
        cases = (
            ("usage", valid_row(agent_type=self.HASH, tools=self.HASH + ";mcp:" + self.HASH)),
            ("usage", valid_row(tools="")),
            ("context", context_row(agent_type=self.HASH)),
            ("skills", skill_row(skill=self.HASH)),
            ("catalog", catalog_row(name=self.HASH)),
        )
        for stream, row in cases:
            with self.subTest(stream=stream):
                self.assertEqual(self.problems(stream, row), [])

    def test_slug_names_fail_without_echoing_them(self):
        cases = (
            ("usage", valid_row(tools=self.HASH), "agent_type", "Explore"),
            ("usage", valid_row(), "tools", "Bash;Read"),
            ("usage", valid_row(), "tools", "mcp:Explore"),
            ("context", context_row(), "agent_type", "Explore"),
            ("skills", skill_row(), "skill", "client-review"),
            ("catalog", catalog_row(), "name", "client-review"),
        )
        for stream, row, column, value in cases:
            with self.subTest(stream=stream, column=column):
                problems = self.problems(stream, dict(row, **{column: value}))
                self.assertEqual(problems, ["%s 0: column %s is invalid"
                                            % (guard.SCHEMAS[stream].label, column)])

    def test_slug_names_still_pass_when_hashing_is_off(self):
        self.assertEqual(guard.validate_stream("skills", [skill_row()]), [])


if __name__ == "__main__":
    unittest.main()
