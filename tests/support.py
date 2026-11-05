import os
import shutil
import tempfile
import time

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PROJECTS = os.path.join(FIXTURES, "projects")
TRANSCRIPT_CANARIES = (
    "CANARY_TITLE_7f3", "CANARY_DESC_7f3", "CANARY_PATH_7f3",
    "CANARY_BRANCH_7f3", "CANARY_PROMPT_7f3", "CANARY_TOOLINPUT_7f3",
    "CANARY_REPLY_7f3",
)
CONTEXT_CANARIES = (
    "CANARY_SYSPROMPT_7f3", "CANARY_INSTRUCTION_7f3", "CANARY_INSTRPATH_7f3",
    "CANARY_SKILLLIST_7f3", "CANARY_SKILLNAME_7f3", "CANARY_SKILLBODY_7f3",
    "CANARY_SKILLPATH_7f3", "CANARY_AGENTLINE_7f3", "CANARY_AGENTNAME_7f3",
    "CANARY_DEFERREDLINE_7f3", "CANARY_TOOLNAME_7f3", "CANARY_TOOLDESC_7f3",
    "CANARY_MCPNAME_7f3", "CANARY_MCPBLOCK_7f3", "CANARY_HOOKCONTEXT_7f3",
)
CANARIES = TRANSCRIPT_CANARIES + CONTEXT_CANARIES
SALT = bytes(range(32))
IDLE_SECONDS = 24 * 3600
ALPHA = "-home-example-user-alpha"
BETA = "-home-example-user-beta"
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
B2_LINE = (
    '{"parentUuid": null, "isSidechain": false, "type": "assistant", '
    '"sessionId": "22222222-2222-4222-8222-222222222222", '
    '"cwd": "/home/example-user/CANARY_PATH_7f3", '
    '"gitBranch": "CANARY_BRANCH_7f3", "version": "2.1.0", '
    '"timestamp": "2026-03-05T08:20:00Z", "uuid": "msg_B2-u", '
    '"message": {"id": "msg_B2", "type": "message", "role": "assistant", '
    '"model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}], '
    '"usage": {"input_tokens": 6, "cache_read_input_tokens": 7, '
    '"cache_creation_input_tokens": 8, "output_tokens": 9, '
    '"service_tier": "standard"}}, "effort": "low"}'
)


class TempDir:
    def __init__(self):
        self.path = tempfile.mkdtemp()

    def copy_projects(self) -> str:
        target = os.path.join(self.path, "projects")
        shutil.copytree(PROJECTS, target)
        for folder, _dirs, files in os.walk(target):
            for name in files:
                mark_idle(os.path.join(folder, name))
        return target

    def join(self, *parts) -> str:
        return os.path.join(self.path, *parts)

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


def complete_partial_line(projects_root: str) -> None:
    path = os.path.join(projects_root, ALPHA, S2 + ".jsonl")
    with open(path) as handle:
        tail = handle.read().rsplit("\n", 1)[-1]
    assert B2_LINE.startswith(tail)
    with open(path, "a") as handle:
        handle.write(B2_LINE[len(tail):] + "\n")
    mark_idle(path)


IDLE_AT = time.time() - IDLE_SECONDS


def mark_idle(path: str) -> None:
    """Backdate a transcript so its last message counts as finished.

    Every file gets the same time, which on macOS also becomes its birth
    time, so fixture files tie on age and are read in path order.
    """
    os.utime(path, (IDLE_AT, IDLE_AT))


def read_all_bytes(root: str) -> bytes:
    data = b""
    for folder, _dirs, files in os.walk(root):
        for name in files:
            with open(os.path.join(folder, name), "rb") as handle:
                data += handle.read()
    return data
