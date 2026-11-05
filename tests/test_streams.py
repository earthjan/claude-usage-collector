import json
import os
import shutil
import time
import unittest
from datetime import timedelta

from usage_collector import extract, guard
from usage_collector import state as state_module
from usage_collector.state import State

from tests import support
from tests.support import ALPHA, BETA, S1, S3
from tests.test_extract import ExtractTestCase, TODAY, h

S4 = "44444444-4444-4444-8444-444444444444"
FORK = "f0f0f0f0f0f0"
EXPLORE = "a1b2c3d4e5f6"
NESTED = "0a0a0a0a0a0a"
ODD_SKILL = "Odd Skill CANARY_SKILLNAME_7f3"
ODD_AGENT = "Custom Agent CANARY_AGENTNAME_7f3"


def zero_parts(**parts):
    columns = dict.fromkeys(guard.CONTEXT_PARTS, "0")
    columns.update(parts)
    return columns


class StreamTestCase(ExtractTestCase):
    def collect_streams(self, today=TODAY, **overrides):
        result = extract.extract([self.root], self.state, self.settings(**overrides))
        extract.apply(result, self.state, today)
        return result.streams

    def events(self, streams):
        return [(row["agent"], row["event"], row["ts"], row["first_turn_context"])
                for row in streams["context"]]

    def copy_session(self, source_session, project, target_session, extra=""):
        source = os.path.join(self.root, ALPHA, source_session + ".jsonl")
        target = os.path.join(self.root, project, target_session + ".jsonl")
        with open(source) as handle, open(target, "w") as out:
            out.write(handle.read() + extra)
        support.mark_idle(target)
        return target


class ContextEventTest(StreamTestCase):
    def test_start_event_measures_everything_attached_before_the_first_turn(self):
        start = self.collect_streams()["context"][0]
        expected = {
            "ts": "2026-03-01T10:00:05Z", "account": "work",
            "machine": h("example-host"), "project": h(ALPHA), "session": h(S1),
            "agent": "", "is_subagent": "0", "agent_type": "", "event": "start",
            "first_turn_context": "1210",
        }
        expected.update(zero_parts(
            system_prompt_chars="1500", instr_user_chars="100",
            instr_project_chars="200", instr_local_chars="30",
            instr_memory_chars="40", instr_other_chars="50", instr_files="5",
            skill_listing_chars="300", skill_count="3",
            agent_listing_chars="100", agent_count="2",
            deferred_tools_chars="179", deferred_tool_count="3",
            mcp_chars="250", mcp_server_count="1", hook_context_chars="100",
        ))
        self.assertEqual(start, expected)

    def test_compact_event_measures_what_was_attached_after_the_boundary(self):
        compact = self.collect_streams()["context"][1]
        self.assertEqual(
            {key: compact[key] for key in ("ts", "event", "first_turn_context")},
            {"ts": "2026-03-01T23:59:59Z", "event": "compact", "first_turn_context": "9005"},
        )
        self.assertEqual({key: compact[key] for key in guard.CONTEXT_PARTS}, zero_parts(
            instr_user_chars="100", instr_files="1",
            skill_listing_chars="120", skill_count="2",
        ))

    def test_one_event_per_file_start_and_per_compaction(self):
        self.assertEqual(self.events(self.collect_streams()), [
            ("", "start", "2026-03-01T10:00:05Z", "1210"),
            ("", "compact", "2026-03-01T23:59:59Z", "9005"),
            ("", "start", "2026-03-05T08:15:42Z", "6"),
            ("", "start", "2026-03-02T14:45:10Z", "50"),
            (h(EXPLORE), "start", "2026-03-01T10:00:30Z", "5100"),
            (h(FORK), "fork", "2026-03-01T10:02:00Z", "1464"),
            (h(NESTED), "start", "2026-03-01T11:00:00Z", "7"),
        ])

    def test_subagent_event_carries_its_identity(self):
        rows = {row["agent"]: row for row in self.collect_streams()["context"]}
        explore = rows[h(EXPLORE)]
        self.assertEqual(
            (explore["is_subagent"], explore["agent_type"], explore["system_prompt_chars"]),
            ("1", "Explore", "200"),
        )

    def test_fork_gets_its_own_fork_event_and_never_a_copy_of_the_parent_start(self):
        rows = self.collect_streams()["context"]
        parent_starts = [row for row in rows
                         if row["agent"] == "" and row["session"] == h(S1)
                         and row["event"] == "start"]
        self.assertEqual(len(parent_starts), 1)
        fork = [row for row in rows if row["agent"] == h(FORK)]
        self.assertEqual([row["event"] for row in fork], ["fork"])
        self.assertEqual({key: fork[0][key] for key in guard.CONTEXT_PARTS},
                         zero_parts(system_prompt_chars="300", hook_context_chars="40"))

    def test_forked_session_gets_one_start_after_its_copied_history(self):
        new_turn = support.B2_LINE.replace("msg_B2", "msg_FORKNEW") + "\n"
        for label, collect_first in (("later run", True), ("same run", False)):
            with self.subTest(label):
                self.state = State({})
                first = self.collect_streams() if collect_first else None
                self.copy_session(S1, BETA, S4, extra=new_turn)
                rows = self.collect_streams()["context"]
                if first is not None:
                    rows = first["context"] + rows
                forked = [row for row in rows if row["session"] == h(S4)]
                self.assertEqual([(row["event"], row["first_turn_context"]) for row in forked],
                                 [("fork", "21")])
                self.assertEqual({key: forked[0][key] for key in guard.CONTEXT_PARTS},
                                 zero_parts())
                self.assertEqual(len(rows), 8)
                os.remove(os.path.join(self.root, BETA, S4 + ".jsonl"))

    def test_fork_event_measures_only_what_the_fork_attached_after_its_copies(self):
        own = json.dumps({"type": "attachment", "uuid": "att-fork-own", "attachment": {
            "type": "hook_additional_context", "content": ["y" * 40]}}) + "\n"
        self.copy_session(S1, BETA, S4,
                          extra=own + support.B2_LINE.replace("msg_B2", "msg_FORKNEW") + "\n")
        forked = [row for row in self.collect_streams()["context"]
                  if row["session"] == h(S4)]
        self.assertEqual([(row["event"], row["first_turn_context"]) for row in forked],
                         [("fork", "21")])
        self.assertEqual({key: forked[0][key] for key in guard.CONTEXT_PARTS},
                         zero_parts(hook_context_chars="40"))

    def test_copied_history_is_not_read_again_once_its_marks_expire(self):
        self.copy_session(S1, BETA, S4)
        self.collect_streams()
        later = TODAY + timedelta(days=state_module.KEEP_SEEN_DAYS + 1)
        self.collect_streams(today=later)
        self.assertEqual(self.collect_streams(today=later)["context"], [])

    def test_fork_waiting_across_runs_for_its_own_turn_stays_a_fork(self):
        self.copy_session(S1, BETA, S4)
        self.collect_streams()
        path = os.path.join(self.root, BETA, S4 + ".jsonl")
        with open(path, "a") as handle:
            handle.write(support.B2_LINE.replace("msg_B2", "msg_FORKLATE") + "\n")
        support.mark_idle(path)
        rows = self.collect_streams()["context"]
        self.assertEqual([(row["session"], row["event"]) for row in rows], [(h(S4), "fork")])

    def test_copied_compaction_after_an_own_turn_is_not_a_compact_event(self):
        path = os.path.join(self.root, BETA, S4 + ".jsonl")
        with open(path, "w") as handle:
            handle.write(support.B2_LINE.replace("msg_B2", "msg_OWN") + "\n")
            with open(os.path.join(self.root, ALPHA, S1 + ".jsonl")) as source:
                handle.write(source.read())
            handle.write(support.B2_LINE.replace("msg_B2", "msg_AFTER") + "\n")
        support.mark_idle(path)
        rows = [row for row in self.collect_streams()["context"]
                if row["session"] == h(S4)]
        self.assertEqual([row["event"] for row in rows], ["start"])

    def test_forked_session_without_a_turn_of_its_own_adds_no_event(self):
        self.collect_streams()
        self.copy_session(S1, BETA, S4)
        self.assertEqual(self.collect_streams()["context"], [])

    def test_attachments_waiting_for_their_first_turn_are_counted_once_it_lands(self):
        self.collect_streams()
        path = os.path.join(self.root, BETA, S4 + ".jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "attachment", "uuid": "att-wait",
                                     "timestamp": "2026-03-06T09:00:00Z", "attachment": {
                                         "type": "hook_additional_context",
                                         "content": ["CANARY_HOOKCONTEXT_7f3 waiting"]}}) + "\n")
        support.mark_idle(path)
        self.assertEqual(self.collect_streams()["context"], [])
        with open(path, "a") as handle:
            handle.write(support.B2_LINE.replace("msg_B2", "msg_WAIT") + "\n")
        support.mark_idle(path)
        rows = self.collect_streams()["context"]
        self.assertEqual([(row["session"], row["hook_context_chars"]) for row in rows],
                         [(h(S4), "30")])

    def test_compaction_after_an_earlier_run_is_measured_on_the_next_run(self):
        self.collect_streams()
        path = os.path.join(self.root, BETA, S3 + ".jsonl")
        with open(path, "a") as handle:
            handle.write('{"type":"system","subtype":"compact_boundary"}\n')
            handle.write(json.dumps({"type": "attachment", "uuid": "att-late", "attachment": {
                "type": "prompt_snapshot", "systemPrompt": ["CANARY_SYSPROMPT_7f3"]}}) + "\n")
        support.mark_idle(path)
        self.assertEqual(self.collect_streams()["context"], [])
        with open(path, "a") as handle:
            handle.write(support.B2_LINE.replace("msg_B2", "msg_LATE") + "\n")
        support.mark_idle(path)
        rows = self.collect_streams()["context"]
        self.assertEqual([(row["event"], row["system_prompt_chars"], row["first_turn_context"])
                          for row in rows], [("compact", "20", "21")])

    def test_a_turn_without_context_tokens_leaves_the_event_open(self):
        path = os.path.join(self.root, BETA, S4 + ".jsonl")
        zero = json.loads(support.B2_LINE.replace("msg_B2", "msg_ZERO"))
        zero["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
        with open(path, "w") as handle:
            handle.write('{"type":"system","subtype":"compact_boundary"}\n')
            handle.write(json.dumps(zero) + "\n")
            handle.write(json.dumps({"type": "attachment", "uuid": "att-zero",
                                     "attachment": {"type": "hook_additional_context",
                                                    "content": ["x" * 12]}}) + "\n")
            handle.write(support.B2_LINE.replace("msg_B2", "msg_REAL") + "\n")
        support.mark_idle(path)
        rows = [row for row in self.collect_streams()["context"] if row["session"] == h(S4)]
        self.assertEqual(
            [(row["event"], row["hook_context_chars"], row["first_turn_context"])
             for row in rows], [("start", "12", "21")])

    def test_second_run_finds_no_new_events(self):
        self.collect_streams()
        streams = self.collect_streams()
        self.assertEqual({name: len(rows) for name, rows in streams.items()},
                         {"usage": 0, "context": 0, "skills": 0, "catalog": 0})


PARENT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EARLY_FORK = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def turn(message_id, ts):
    return support.B2_LINE.replace("msg_B2", message_id).replace(
        "2026-03-05T08:20:00Z", ts) + "\n"


def attachment(uuid, kind, field, length):
    return json.dumps({"type": "attachment", "uuid": uuid, "attachment": {
        "type": kind, field: ["z" * length]}}) + "\n"


@unittest.skipUnless(hasattr(os.stat_result, "st_birthtime"), "needs file birth times")
class ForkReadOrderTest(StreamTestCase):
    def write(self, session, text, age_seconds):
        path = os.path.join(self.root, BETA, session + ".jsonl")
        with open(path, "w") as handle:
            handle.write(text)
        when = time.time() - age_seconds
        os.utime(path, (when, when))

    def test_a_fork_whose_name_sorts_first_is_still_read_after_its_parent(self):
        prompt = attachment("att-prompt", "prompt_snapshot", "systemPrompt", 400)
        first = turn("msg_P1", "2026-03-06T09:00:00Z")
        self.write(PARENT, prompt + first + turn("msg_P2", "2026-03-06T09:05:00Z"),
                   2 * support.IDLE_SECONDS)
        self.write(EARLY_FORK, prompt + first
                   + attachment("att-hook", "hook_additional_context", "content", 40)
                   + turn("msg_F3", "2026-03-06T10:00:00Z"), support.IDLE_SECONDS)
        rows = [row for row in self.collect_streams()["context"]
                if row["session"] in (h(PARENT), h(EARLY_FORK))]
        self.assertEqual(
            [(row["session"], row["event"], row["system_prompt_chars"],
              row["hook_context_chars"]) for row in rows],
            [(h(PARENT), "start", "400", "0"), (h(EARLY_FORK), "fork", "0", "40")])


class SkillUseTest(StreamTestCase):
    def test_each_skill_is_counted_once_per_session_and_agent(self):
        rows = self.collect_streams()["skills"]
        self.assertEqual(rows, [
            {"ts": "2026-03-01T12:00:02Z", "account": "work", "machine": h("example-host"),
             "project": h(ALPHA), "session": h(S1), "agent": "", "skill": "tdd"},
            {"ts": "2026-03-01T12:00:02Z", "account": "work", "machine": h("example-host"),
             "project": h(ALPHA), "session": h(S1), "agent": "", "skill": h(ODD_SKILL)},
            {"ts": "2026-03-02T15:00:00Z", "account": "work", "machine": h("example-host"),
             "project": h(BETA), "session": h(S3), "agent": "", "skill": "tdd"},
        ])

    def test_a_later_listing_of_the_same_skills_adds_nothing(self):
        self.collect_streams()
        path = os.path.join(self.root, BETA, S3 + ".jsonl")
        with open(path, "a") as handle:
            handle.write(json.dumps({"type": "attachment", "uuid": "att-again",
                                     "timestamp": "2026-03-02T16:00:00Z", "attachment": {
                                         "type": "invoked_skills",
                                         "skills": [{"name": "tdd", "content": "x"}]}}) + "\n")
        support.mark_idle(path)
        self.assertEqual(self.collect_streams()["skills"], [])

    def test_copied_history_in_a_forked_session_adds_no_skill_use(self):
        self.collect_streams()
        self.copy_session(S1, BETA, S4)
        self.assertEqual(self.collect_streams()["skills"], [])


class CatalogTest(StreamTestCase):
    def test_listed_names_once_per_day_and_kind(self):
        rows = self.collect_streams()["catalog"]
        self.assertEqual([(row["date"], row["kind"], row["name"]) for row in rows], [
            ("2026-03-01", "skill", "tdd"),
            ("2026-03-01", "skill", "plugin-x:review"),
            ("2026-03-01", "skill", h(ODD_SKILL)),
            ("2026-03-01", "agent", "Explore"),
            ("2026-03-01", "agent", "general-purpose"),
            ("2026-03-01", "agent", "Plan"),
            ("2026-03-01", "agent", h(ODD_AGENT)),
            ("2026-03-02", "skill", "tdd"),
            ("2026-03-02", "skill", "beta-only"),
        ])
        self.assertEqual({(row["account"], row["machine"]) for row in rows},
                         {("work", h("example-host"))})

    def test_hash_names_hashes_skill_and_catalog_names(self):
        streams = self.collect_streams(hash_names=True)
        self.assertEqual(streams["skills"][0]["skill"], h("tdd"))
        self.assertEqual(streams["catalog"][3]["name"], h("Explore"))


class BackfillTest(StreamTestCase):
    def test_v1_state_backfills_new_streams_without_repeating_usage_rows(self):
        first = self.collect_streams()
        path = self.tmp.join("state.json")
        self.state.save(path)
        with open(path) as handle:
            data = json.load(handle)
        v1 = {key: data[key] for key in (
            "files", "seen", "last_run", "last_rows", "total_rows", "crosscheck_month")}
        v1["files"] = {
            name: {"offset": entry["offset"], "compact": entry["compact"]}
            for name, entry in data["files"].items()
        }
        with open(path, "w") as handle:
            json.dump(v1, handle)
        self.state = State.load(path)
        again = self.collect_streams()
        self.assertEqual(again["usage"], [])
        for stream in ("context", "skills", "catalog"):
            with self.subTest(stream=stream):
                self.assertTrue(first[stream])
                self.assertEqual(again[stream], first[stream])


class OddTypeTest(StreamTestCase):
    def test_list_or_object_types_are_ignored_and_the_rest_still_collects(self):
        path = os.path.join(self.root, BETA, S4 + ".jsonl")
        odd = [
            {"type": "attachment", "uuid": "att-odd-1", "timestamp": "2026-03-06T09:00:00Z",
             "attachment": {"type": ["skill_listing"], "names": ["tdd"]}},
            {"type": "attachment", "uuid": "att-odd-2", "timestamp": "2026-03-06T09:00:00Z",
             "attachment": {"type": {"kind": "x"}}},
            {"type": "attachment", "uuid": "att-odd-3", "timestamp": "2026-03-06T09:00:00Z",
             "attachment": {"type": "instructions", "files": [
                 {"type": ["User"], "content": "CANARY_INSTRUCTION_7f3"}]}},
        ]
        with open(path, "w") as handle:
            handle.writelines(json.dumps(record) + "\n" for record in odd)
            handle.write(support.B2_LINE.replace("msg_B2", "msg_ODD") + "\n")
        support.mark_idle(path)
        rows = [row for row in self.collect_streams()["context"]
                if row["session"] == h(S4)]
        self.assertEqual(
            [(row["skill_count"], row["instr_other_chars"], row["instr_files"]) for row in rows],
            [("0", "22", "1")])


class PrivacyTest(StreamTestCase):
    def test_every_stream_passes_its_guard_and_carries_no_canary(self):
        streams = self.collect_streams()
        self.assertEqual(guard.validate_streams(streams), [])
        text = repr(streams)
        for canary in support.CANARIES:
            self.assertNotIn(canary, text)

    def test_subagent_copy_without_meta_adds_no_second_event(self):
        original = os.path.join(self.root, ALPHA, S1, "subagents")
        copy = os.path.join(self.root, ALPHA, S4, "subagents")
        os.makedirs(copy)
        shutil.copy2(os.path.join(original, "agent-" + EXPLORE + ".jsonl"), copy)
        rows = self.collect_streams()["context"]
        self.assertEqual(len([row for row in rows if row["agent"] == h(EXPLORE)]), 1)


if __name__ == "__main__":
    unittest.main()
