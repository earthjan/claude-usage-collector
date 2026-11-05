import json
import os
import unittest

from usage_collector import audit, sinks

from tests import support
from tests.test_guard import catalog_row, context_row, skill_row, valid_row
from tests.test_sinks import TOTALS

CANARY = "CANARY_AUDIT_7f3"
DEMO_STORE = os.path.join(support.FIXTURES, "demo-store")
DAY = "work/2026/03/2026-03-01"


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def read_text(path):
    with open(path) as handle:
        return handle.read()


class AuditStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.store = self.tmp.join("store")
        sink = sinks.FolderSink(self.store, "work")
        sink.write_streams({
            "usage": [valid_row(ts="2026-03-01T10:00:00Z")],
            "context": [context_row(ts="2026-03-01T10:00:00Z")],
            "skills": [skill_row(ts="2026-03-01T09:00:00Z")],
            "catalog": [catalog_row(date="2026-03-01")],
        })
        sink.write_check("2026-W09", TOTALS, "laptop-1")
        sink.write_check("2026-03", TOTALS, "laptop-1")
        write_text(self.join(".gitattributes"), "*.csv merge=union\n")

    def tearDown(self):
        self.tmp.cleanup()

    def join(self, relative):
        return os.path.join(self.store, relative)

    def test_a_store_written_by_the_tool_passes(self):
        self.assertEqual(audit.audit_store(self.store), [])

    def test_the_demo_store_passes(self):
        self.assertEqual(audit.audit_store(DEMO_STORE), [])

    def test_a_month_check_written_by_an_older_version_passes(self):
        write_text(self.join("checks/work/2026-02.json"), json.dumps(TOTALS))
        self.assertEqual(audit.audit_store(self.store), [])

    def test_the_git_folder_is_not_audited(self):
        write_text(self.join(".git/HEAD"), "ref: refs/heads/main\n")
        self.assertEqual(audit.audit_store(self.store), [])

    def test_a_stray_file_fails_by_path(self):
        write_text(self.join("notes.txt"), CANARY)
        self.assertEqual(audit.audit_store(self.store),
                         ["notes.txt: not part of the store layout"])

    def test_paths_outside_the_layout_fail(self):
        for relative in (
            "work/2026/04/2026-03-01.csv", "work/2026/03/2026-03-32.csv",
            "work/2026/03/2026-03-01.notes.csv", "work/2026/03/2026-03-01.csv.tmp",
            "checks/2026/03/2026-03-01.csv", ".hidden/2026/03/2026-03-01.csv",
            "work/2026/03/.DS_Store", "checks/work/laptop-1/2026-W99.json",
            "checks/work/laptop-1/notes.json", "checks/work/2026-W09.json",
            "checks/work/laptop-1/extra/2026-03.json", "work/.gitattributes",
        ):
            with self.subTest(relative=relative):
                write_text(self.join(relative), read_text(self.join(DAY + ".csv")))
                problems = audit.audit_store(self.store)
                self.assertIn(relative + ": not part of the store layout", problems)
                os.remove(self.join(relative))

    def test_a_link_fails_even_when_its_name_fits(self):
        target = self.tmp.join("elsewhere.csv")
        write_text(target, read_text(self.join(DAY + ".csv")))
        os.symlink(target, self.join("work/2026/03/2026-03-02.csv"))
        self.assertEqual(audit.audit_store(self.store),
                         ["work/2026/03/2026-03-02.csv: is a link, not a file"])

    def test_a_bad_header_fails(self):
        path = self.join(DAY + ".skills.csv")
        lines = read_text(path).splitlines(True)
        write_text(path, lines[0].replace("skill", "notes") + "".join(lines[1:]))
        self.assertEqual(audit.audit_store(self.store),
                         [DAY + ".skills.csv: header does not match its schema"])

    def test_an_empty_csv_fails_on_its_header(self):
        write_text(self.join(DAY + ".catalog.csv"), "")
        self.assertEqual(audit.audit_store(self.store),
                         [DAY + ".catalog.csv: header does not match its schema"])

    def test_a_tampered_row_fails_without_echoing_the_value(self):
        path = self.join(DAY + ".csv")
        write_text(path, read_text(path).replace("claude-opus-5-5", CANARY + " private"))
        problems = audit.audit_store(self.store)
        self.assertEqual(problems, [DAY + ".csv: row 0: column model is invalid"])
        self.assertNotIn(CANARY, "\n".join(problems))

    def test_a_row_with_an_extra_cell_fails(self):
        path = self.join(DAY + ".context.csv")
        write_text(path, read_text(path).rstrip("\n") + "," + CANARY + "\n")
        problems = audit.audit_store(self.store)
        self.assertEqual(problems, [DAY + ".context.csv: context row 0: unknown column"])
        self.assertNotIn(CANARY, "\n".join(problems))

    def test_a_file_that_is_not_text_fails(self):
        with open(self.join(DAY + ".csv"), "ab") as handle:
            handle.write(b"\xff\xfe\x00")
        self.assertEqual(audit.audit_store(self.store),
                         [DAY + ".csv: could not be read as CSV"])

    def test_a_bad_check_file_fails(self):
        path = self.join("checks/work/laptop-1/2026-W09.json")
        tampered = {"2026-03-01": {"claude-opus-5-5": {"input": CANARY}}}
        write_text(path, json.dumps(tampered))
        problems = audit.audit_store(self.store)
        self.assertEqual(problems, [
            "checks/work/laptop-1/2026-W09.json: day 0 model 0: counts have the wrong keys"])
        self.assertNotIn(CANARY, "\n".join(problems))

    def test_a_check_file_that_is_not_json_fails(self):
        write_text(self.join("checks/work/laptop-1/2026-03.json"), CANARY)
        problems = audit.audit_store(self.store)
        self.assertEqual(problems,
                         ["checks/work/laptop-1/2026-03.json: could not be read as JSON"])

    def test_a_check_file_with_a_duplicate_key_fails_without_echoing_it(self):
        hidden = '{"%s": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4}}' % CANARY
        valid = json.dumps(TOTALS["2026-03-01"])
        write_text(self.join("checks/work/laptop-1/2026-03.json"),
                   '{"2026-03-01": %s, "2026-03-01": %s}' % (hidden, valid))
        problems = audit.audit_store(self.store)
        self.assertEqual(problems,
                         ["checks/work/laptop-1/2026-03.json: has a duplicate key"])
        self.assertNotIn(CANARY, "\n".join(problems))

    def test_a_modified_gitattributes_fails(self):
        write_text(self.join(".gitattributes"), "*.csv merge=union\n*.txt -diff\n")
        self.assertEqual(audit.audit_store(self.store),
                         [".gitattributes: content is not what the tool writes"])

    def test_a_slug_skill_name_fails_when_names_are_hashed(self):
        self.assertEqual(audit.audit_store(self.store), [])
        problems = audit.audit_store(self.store, hashed_names=True)
        self.assertIn(DAY + ".skills.csv: skills row 0: column skill is invalid", problems)
        self.assertNotIn("plugin-x", "\n".join(problems))

    def test_audit_paths_checks_only_the_paths_given(self):
        write_text(self.join("notes.txt"), CANARY)
        self.assertEqual(audit.audit_paths(self.store, [DAY + ".csv"]), [])
        self.assertEqual(audit.audit_paths(self.store, ["notes.txt", DAY + ".csv"]),
                         ["notes.txt: not part of the store layout"])

    def test_audit_paths_skips_a_path_that_no_longer_exists(self):
        self.assertEqual(audit.audit_paths(self.store, ["work/2026/03/2026-03-09.csv"]), [])

    def test_the_audit_changes_nothing(self):
        write_text(self.join("notes.txt"), CANARY)
        before = support.read_all_bytes(self.store)
        audit.audit_store(self.store)
        self.assertEqual(support.read_all_bytes(self.store), before)


if __name__ == "__main__":
    unittest.main()
