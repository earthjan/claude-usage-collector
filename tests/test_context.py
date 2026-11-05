import unittest

from usage_collector import guard
from usage_collector.context import Composition


def text(length, start="CANARY_TEXT_7f3"):
    return (start + "x" * length)[:length]


def measure(*attachments):
    composition = Composition()
    for attachment in attachments:
        composition.add(attachment)
    return composition.columns()


def nonzero(columns):
    return {key: value for key, value in columns.items() if value != "0"}


class CompositionTest(unittest.TestCase):
    def test_nothing_attached_gives_zero_for_every_part(self):
        self.assertEqual(measure(), dict.fromkeys(guard.CONTEXT_PARTS, "0"))

    def test_instruction_files_are_split_by_type_and_counted(self):
        files = [
            {"path": "/x/CANARY_PATH_7f3", "type": "User", "content": text(100)},
            {"path": "/x/y", "type": "Project", "content": text(200)},
            {"path": "/x/y", "type": "Local", "content": text(30)},
            {"path": "/x/y", "type": "AutoMem", "content": text(40)},
            {"path": "/x/y", "type": "Managed", "content": text(50)},
            {"path": "/x/y", "type": "Project", "content": None},
            "not a file",
        ]
        self.assertEqual(nonzero(measure({"type": "instructions", "files": files})), {
            "instr_user_chars": "100", "instr_project_chars": "200",
            "instr_local_chars": "30", "instr_memory_chars": "40",
            "instr_other_chars": "50", "instr_files": "6",
        })

    def test_a_later_snapshot_or_instruction_set_replaces_the_earlier_one(self):
        columns = measure(
            {"type": "prompt_snapshot", "systemPrompt": [text(1000), text(500)]},
            {"type": "instructions", "files": [{"type": "User", "content": text(10)}]},
            {"type": "prompt_snapshot", "systemPrompt": [text(700)]},
            {"type": "instructions", "files": [{"type": "User", "content": text(20)}]},
        )
        self.assertEqual(nonzero(columns), {
            "system_prompt_chars": "700", "instr_user_chars": "20", "instr_files": "1",
        })

    def test_listings_add_up_their_text_and_count_names(self):
        columns = measure(
            {"type": "skill_listing", "content": text(300), "skillCount": 3,
             "names": ["a", "b", "c"], "isInitial": True},
            {"type": "skill_listing", "content": text(20), "names": ["d", "e"]},
            {"type": "agent_listing_delta", "addedTypes": ["Explore", "Plan"],
             "addedLines": [text(60), text(40)], "builtInTypes": ["Explore"]},
            {"type": "agent_listing_delta", "addedTypes": ["Explore"], "addedLines": [7]},
        )
        self.assertEqual(nonzero(columns), {
            "skill_listing_chars": "320", "skill_count": "5",
            "agent_listing_chars": "100", "agent_count": "2",
        })

    def test_deferred_tools_from_deltas_and_records_count_each_name_once(self):
        columns = measure(
            {"type": "deferred_tools_delta", "addedNames": ["WebFetch", "mcp__s__x"],
             "addedLines": [text(70), text(30)]},
            {"type": "deferred_tools_record", "entries": [
                {"name": "NotebookEdit", "description": text(50),
                 "input_schema": {"type": "object"}},
                {"name": "WebFetch", "description": text(10)},
                "not an entry",
            ]},
        )
        # Record entries count name + description + compact schema JSON:
        # 12 + 50 + 17 and 8 + 10 + 0.
        self.assertEqual(nonzero(columns), {
            "deferred_tools_chars": "197", "deferred_tool_count": "3",
        })

    def test_mcp_blocks_and_hook_context(self):
        columns = measure(
            {"type": "mcp_instructions_delta", "addedNames": ["server-a"],
             "addedBlocks": [text(250)]},
            {"type": "hook_additional_context", "content": [text(80), text(20)],
             "hookName": "SessionStart"},
        )
        self.assertEqual(nonzero(columns), {
            "mcp_chars": "250", "mcp_server_count": "1", "hook_context_chars": "100",
        })

    def test_other_or_malformed_attachments_are_ignored(self):
        columns = measure(
            {"type": "date", "content": text(99)},
            {"type": "prompt_snapshot", "systemPrompt": "not a list"},
            {"type": "hook_additional_context", "content": None},
            {"type": "skill_listing", "content": 5, "skillCount": True},
            "not an attachment",
        )
        self.assertEqual(nonzero(columns), {})

    def test_fits_only_while_every_part_stays_under_the_row_limit(self):
        composition = Composition()
        listing = {"type": "skill_listing", "content": "", "skillCount": guard.ROW_INT_LIMIT}
        composition.add(listing)
        self.assertTrue(composition.fits())
        composition.add(dict(listing, skillCount=1))
        self.assertFalse(composition.fits())


if __name__ == "__main__":
    unittest.main()
