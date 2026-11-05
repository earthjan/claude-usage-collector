import json
import os
import shutil
import unittest
from datetime import date

from usage_collector import extract, guard
from usage_collector.hashing import hash_value
from usage_collector.state import State

from tests import support
from tests.support import ALPHA, BETA, S1, S2, S3, SALT

TODAY = date(2026, 3, 10)
S0 = "00000000-0000-4000-8000-000000000000"


def assistant_line(message_id, ts, output, tool=None):
    content = [{"type": "text", "text": "CANARY_REPLY_7f3"}]
    if tool:
        content = [{"type": "tool_use", "name": tool, "input": {}}]
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {
            "id": message_id, "model": "claude-opus-5-5", "content": content,
            "usage": {"input_tokens": 1, "output_tokens": output},
        },
    })


def user_line(ts):
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"content": "CANARY_PROMPT_7f3"}})


def h(value):
    return hash_value(SALT, value)


class ExtractTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.root = self.tmp.copy_projects()
        self.state = State({})

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **overrides):
        values = dict(
            salt=SALT, account="work", hostname="example-host",
            machine_label=None, round_ts_to_hour=False,
            hash_agent_types=False,
        )
        values.update(overrides)
        return extract.Settings(**values)

    def collect(self, **overrides):
        result = extract.extract([self.root], self.state, self.settings(**overrides))
        extract.apply(result, self.state, TODAY)
        return result.rows

    def by_ts(self, rows):
        return {row["ts"]: row for row in rows}


class FullRowTest(ExtractTestCase):
    def test_first_main_thread_row_has_every_column(self):
        rows = self.collect()
        self.assertEqual(rows[0], {
            "ts": "2026-03-01T10:00:05Z", "account": "work",
            "machine": h("example-host"), "project": h(ALPHA),
            "session": h(S1), "agent": "", "is_subagent": "0",
            "agent_type": "", "spawn_depth": "", "is_fork": "0",
            "model": "claude-opus-5-5", "effort": "high",
            "input": "10", "cache_read": "1000", "cache_write": "200",
            "output": "50", "context": "1210", "web_search": "0",
            "web_fetch": "0", "tools": "Bash", "compacted": "0",
        })

    def test_subagent_row_takes_identity_from_meta(self):
        row = self.by_ts(self.collect())["2026-03-01T10:00:30Z"]
        self.assertEqual(
            {key: row[key] for key in (
                "session", "agent", "is_subagent", "agent_type",
                "spawn_depth", "is_fork", "model", "effort", "tools", "context",
            )},
            {
                "session": h(S1), "agent": h("a1b2c3d4e5f6"),
                "is_subagent": "1", "agent_type": "Explore",
                "spawn_depth": "1", "is_fork": "0",
                "model": "claude-haiku-5-5", "effort": "", "tools": "Grep",
                "context": "5100",
            },
        )

    def test_every_extracted_row_passes_the_guard(self):
        self.assertEqual(guard.validate_rows(self.collect()), [])


class SelectionTest(ExtractTestCase):
    def test_rows_in_main_then_subagent_order(self):
        summary = [(row["ts"], row["output"]) for row in self.collect()]
        self.assertEqual(summary, [
            ("2026-03-01T10:00:05Z", "50"),
            ("2026-03-01T10:01:00Z", "20"),
            ("2026-03-01T23:59:59Z", "300"),
            ("2026-03-02T00:00:01Z", "7"),
            ("2026-03-05T08:15:42Z", "4"),
            ("2026-03-02T14:45:10Z", "10"),
            ("2026-03-02T15:10:00Z", "2"),
            ("2026-03-01T10:00:30Z", "80"),
            ("2026-03-01T10:00:40Z", "120"),
            ("2026-03-01T10:02:00Z", "25"),
            ("2026-03-01T11:00:00Z", "3"),
        ])

    def test_fork_copies_of_parent_messages_are_not_counted_twice(self):
        rows = self.collect()
        fork_rows = [row for row in rows if row["agent"] == h("f0f0f0f0f0f0")]
        self.assertEqual(len(fork_rows), 1)
        self.assertEqual(fork_rows[0]["is_fork"], "1")
        self.assertEqual(fork_rows[0]["agent_type"], "fork")
        self.assertEqual(fork_rows[0]["tools"], "Edit")

    def test_tools_merged_sorted_and_web_counts_kept(self):
        row = self.by_ts(self.collect())["2026-03-01T10:01:00Z"]
        self.assertEqual(row["tools"], "Bash;Read")
        self.assertEqual((row["web_search"], row["web_fetch"]), ("2", "1"))

    def test_only_row_after_compact_boundary_is_marked(self):
        rows = self.collect()
        compacted = [row["ts"] for row in rows if row["compacted"] == "1"]
        self.assertEqual(compacted, ["2026-03-01T23:59:59Z"])

    def test_synthetic_rows_are_skipped(self):
        models = {row["model"] for row in self.collect()}
        self.assertNotIn("<synthetic>", models)

    def test_unknown_effort_and_missing_cache_fields(self):
        row = self.by_ts(self.collect())["2026-03-02T15:10:00Z"]
        self.assertEqual(row["effort"], "")
        self.assertEqual(
            (row["input"], row["cache_read"], row["cache_write"], row["context"]),
            ("1", "0", "0", "1"),
        )

    def test_mcp_tool_names_are_hashed(self):
        row = self.by_ts(self.collect())["2026-03-02T14:45:10Z"]
        self.assertEqual(
            row["tools"], "Read;mcp:" + h("mcp__private_server__lookup")
        )
        self.assertEqual(row["project"], h(BETA))
        self.assertEqual(row["session"], h(S3))

    def test_subagent_copy_without_meta_does_not_take_the_row(self):
        original = os.path.join(self.root, ALPHA, S1, "subagents")
        copy = os.path.join(self.root, ALPHA, S0, "subagents")
        os.makedirs(copy)
        shutil.copy2(os.path.join(original, "agent-a1b2c3d4e5f6.jsonl"), copy)
        row = self.by_ts(self.collect())["2026-03-01T10:00:30Z"]
        self.assertEqual(
            (row["agent_type"], row["spawn_depth"], row["session"]),
            ("Explore", "1", h(S1)),
        )

    def test_nested_subagent_folder_is_found(self):
        row = self.by_ts(self.collect())["2026-03-01T11:00:00Z"]
        self.assertEqual(row["agent_type"], "plugin-name:agent-name")
        self.assertEqual(row["spawn_depth"], "2")

    def test_no_canary_reaches_any_row(self):
        text = repr(self.collect())
        for canary in support.CANARIES:
            self.assertNotIn(canary, text)


class SettingsTest(ExtractTestCase):
    def test_round_ts_to_hour(self):
        rows = self.collect(round_ts_to_hour=True)
        self.assertEqual(rows[0]["ts"], "2026-03-01T10:00:00Z")
        self.assertEqual(rows[2]["ts"], "2026-03-01T23:00:00Z")

    def test_hash_agent_types(self):
        row = self.collect(hash_agent_types=True)[7]
        self.assertEqual(row["agent_type"], h("Explore"))

    def test_machine_label_replaces_hostname_hash(self):
        rows = self.collect(machine_label="laptop-1")
        self.assertEqual({row["machine"] for row in rows}, {"laptop-1"})


def raw_line(message_id, model="claude-opus-5-5", usage=None, ts="2026-03-04T10:00:00Z"):
    message = {"id": message_id, "content": [],
               "usage": usage or {"input_tokens": 1, "output_tokens": 2}}
    if model is not None:
        message["model"] = model
    return json.dumps({"type": "assistant", "timestamp": ts, "message": message})


class OddValueTest(ExtractTestCase):
    def collect_lines(self, *lines, meta=None):
        self.collect()
        folder = os.path.join(self.root, BETA, S0)
        path = os.path.join(self.root, BETA, S0 + ".jsonl")
        if meta is not None:
            os.makedirs(os.path.join(folder, "subagents"))
            path = os.path.join(folder, "subagents", "agent-0a0a0a0a0a0a.jsonl")
            with open(path[: -len(".jsonl")] + ".meta.json", "w") as handle:
                json.dump(meta, handle)
        with open(path, "wb") as handle:
            for line in lines:
                handle.write(line.encode("utf-8") + b"\n")
        support.mark_idle(path)
        rows = self.collect()
        self.assertEqual(guard.validate_rows(rows), [])
        return rows

    def test_model_names_are_normalised_or_hashed(self):
        cases = [
            ("claude-opus-5-5", "claude-opus-5-5"),
            ("claude-opus-4-1@20250805", "claude-opus-4-1"),
            ("us.anthropic.claude-opus-4-1-20250805-v1:0", "claude-opus-4-1-20250805-v1:0"),
            ("arn:aws:bedrock:region:000000000000:inference-profile/"
             "us.anthropic.claude-sonnet-4-5-v1:0", "claude-sonnet-4-5-v1:0"),
            ("CANARY-proxy-model", h("CANARY-proxy-model")),
            ("claude opus", h("claude opus")),
            (None, "unknown"),
            ("", "unknown"),
        ]
        lines = [raw_line("msg_M%d" % index, model)
                 for index, (model, _expected) in enumerate(cases)]
        rows = self.collect_lines(*lines)
        self.assertEqual([row["model"] for row in rows], [expected for _m, expected in cases])

    def test_counts_over_the_row_limit_skip_only_that_message(self):
        rows = self.collect_lines(
            raw_line("msg_BIG", usage={"input_tokens": 2 * 10 ** 9, "output_tokens": 1}),
            raw_line("msg_SUM", usage={"input_tokens": 6 * 10 ** 8,
                                       "cache_read_input_tokens": 6 * 10 ** 8}),
            raw_line("msg_OK"),
        )
        self.assertEqual([(row["input"], row["output"]) for row in rows], [("1", "2")])

    def test_spawn_depth_over_the_limit_is_left_empty(self):
        rows = self.collect_lines(
            raw_line("msg_D"), meta={"agentType": "Explore", "spawnDepth": 10 ** 12},
        )
        self.assertEqual(rows[0]["spawn_depth"], "")

    def test_timestamp_out_of_range_and_deep_nesting_are_skipped(self):
        rows = self.collect_lines(
            raw_line("msg_OLD", ts="0001-01-01T00:00:00+05:00"),
            "[" * 100000 + "]" * 100000,
            raw_line("msg_OK"),
        )
        self.assertEqual(len(rows), 1)

    def test_multibyte_text_before_a_partial_line_resumes_at_the_right_byte(self):
        self.collect()
        path = os.path.join(self.root, BETA, S0 + ".jsonl")
        prompt = json.dumps({"type": "user", "message": {"content": "caf\u00e9 \u2603"}},
                            ensure_ascii=False)
        done = raw_line("msg_U1")
        later = raw_line("msg_U2", ts="2026-03-04T11:00:00Z")
        with open(path, "wb") as handle:
            handle.write((prompt + "\n" + done + "\n" + later[:20]).encode("utf-8"))
        support.mark_idle(path)
        self.assertEqual(len(self.collect()), 1)
        with open(path, "ab") as handle:
            handle.write((later[20:] + "\n").encode("utf-8"))
        support.mark_idle(path)
        self.assertEqual([row["ts"] for row in self.collect()], ["2026-03-04T11:00:00Z"])


class IncrementalTest(ExtractTestCase):
    def test_second_run_finds_nothing_new(self):
        self.collect()
        self.assertEqual(self.collect(), [])

    def test_partial_line_is_read_once_complete(self):
        self.collect()
        support.complete_partial_line(self.root)
        rows = self.collect()
        self.assertEqual([row["ts"] for row in rows], ["2026-03-05T08:20:00Z"])
        self.assertEqual(rows[0]["session"], h(S2))

    def test_extract_does_not_touch_state_until_applied(self):
        extract.extract([self.root], self.state, self.settings())
        self.assertEqual(len(self.collect()), 11)

    def test_message_seen_in_earlier_run_is_skipped_in_new_file(self):
        self.collect()
        fork = os.path.join(self.root, BETA, "44444444-4444-4444-8444-444444444444.jsonl")
        source = os.path.join(self.root, ALPHA, S1 + ".jsonl")
        with open(source) as handle, open(fork, "w") as out:
            out.write(handle.read())
        self.assertEqual(self.collect(), [])

    def test_compact_boundary_at_end_of_read_marks_next_run(self):
        path = os.path.join(self.root, BETA, S3 + ".jsonl")
        with open(path, "a") as handle:
            handle.write('{"type":"system","subtype":"compact_boundary"}\n')
        self.collect()
        line = support.B2_LINE.replace("msg_B2", "msg_LATE")
        with open(path, "a") as handle:
            handle.write(line + "\n")
        support.mark_idle(path)
        rows = self.collect()
        self.assertEqual([row["compacted"] for row in rows], ["1"])

    def test_fork_of_compacted_history_does_not_repeat_the_flag(self):
        fork = os.path.join(self.root, BETA, "44444444-4444-4444-8444-444444444444.jsonl")
        source = os.path.join(self.root, ALPHA, S1 + ".jsonl")
        new_line = assistant_line("msg_FORKNEW", "2026-03-03T09:00:00Z", 5)
        for label, collect_first in (("later run", True), ("same run", False)):
            with self.subTest(label):
                self.state = State({})
                if collect_first:
                    self.collect()
                with open(source) as handle, open(fork, "w") as out:
                    out.write(handle.read() + new_line + "\n")
                support.mark_idle(fork)
                rows = self.by_ts(self.collect())
                self.assertEqual(rows["2026-03-03T09:00:00Z"]["compacted"], "0")

    def test_message_split_across_runs_keeps_later_output_and_all_tools(self):
        path = os.path.join(self.root, BETA, "55555555-5555-4555-8555-555555555555.jsonl")
        self.collect()
        with open(path, "w") as handle:
            handle.write(assistant_line("msg_S", "2026-03-04T10:00:00Z", 2, "Read") + "\n")
        self.assertEqual(self.collect(), [])
        with open(path, "a") as handle:
            handle.write(assistant_line("msg_S", "2026-03-04T10:00:00Z", 500, "Bash") + "\n")
            handle.write(user_line("2026-03-04T10:00:09Z") + "\n")
        rows = self.collect()
        self.assertEqual([(row["output"], row["tools"]) for row in rows], [("500", "Bash;Read")])

    def test_unfinished_last_message_is_taken_once_the_file_is_idle(self):
        path = os.path.join(self.root, BETA, "55555555-5555-4555-8555-555555555555.jsonl")
        self.collect()
        with open(path, "w") as handle:
            handle.write(assistant_line("msg_T", "2026-03-04T11:00:00Z", 7) + "\n")
        self.assertEqual(self.collect(), [])
        support.mark_idle(path)
        self.assertEqual([row["output"] for row in self.collect()], ["7"])

    def test_truncated_file_is_read_from_the_start(self):
        self.collect()
        path = os.path.join(self.root, BETA, S3 + ".jsonl")
        line = support.B2_LINE.replace("msg_B2", "msg_NEW")
        with open(path, "w") as handle:
            handle.write(line + "\n")
        support.mark_idle(path)
        self.assertEqual(len(self.collect()), 1)

    def test_missing_source_root_yields_no_rows(self):
        result = extract.extract(
            [self.tmp.join("absent")], self.state, self.settings()
        )
        self.assertEqual(result.rows, [])


if __name__ == "__main__":
    unittest.main()
