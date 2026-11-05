import argparse
import csv
import io
import os
import shutil
import socket
import sys
from datetime import datetime, timezone

from . import __version__, analyze, crosscheck, extract, guard, periods, report
from . import config as config_module
from .doctor import Doctor
from .hashing import SaltError, load_salt
from .sinks import GuardError, SyncError, make_sink
from .state import State, StateError

SAMPLE_ROWS = 3
STREAM_COUNTS = (
    ("context", "context events"), ("skills", "skill uses"), ("catalog", "catalog names"),
)


class Context:
    def __init__(self, config_path: str, environ, now: datetime):
        self.config_path = config_path
        self.environ = environ
        self.now = now

    @property
    def today(self):
        return self.now.date()

    @property
    def state_path(self) -> str:
        return config_module.state_path(self.config_path)

    def config(self) -> dict:
        return config_module.load_config(self.config_path)

    def optional_config(self):
        try:
            return self.config()
        except config_module.ConfigError:
            return None

    def salt(self) -> bytes:
        return load_salt(config_module.salt_path(self.config_path))

    def sink(self, config: dict):
        return make_sink(config, env=dict(self.environ))

    def timestamp(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_init(args, ctx) -> int:
    config_module.init_config(
        ctx.config_path, account=args.account, dest_type=args.dest_type,
        dest=args.dest, schedule=args.schedule, force=args.force,
    )
    print("wrote config to %s" % ctx.config_path)
    return 0


def cmd_collect(args, ctx) -> int:
    config = ctx.config()
    sink = ctx.sink(config)
    if not args.dry_run:
        sink.check_ready()
    state = State.load(ctx.state_path)
    result = extract.extract(config["sources"], state, _settings(config, ctx))
    problems = guard.validate_streams(result.streams)
    if problems:
        raise GuardError(problems, result.files_of_rejected_rows())
    if args.dry_run:
        _print_preview(result.streams)
        return 0
    written = sink.write_streams(result.streams)
    extract.apply(result, state, ctx.today)
    state.record_run(ctx.timestamp(), len(result.rows))
    state.save(ctx.state_path)
    print("collected %d new rows, %s into %d file(s)"
          % (len(result.rows), _stream_counts(result.streams), len(written)))
    return 0


def _stream_counts(streams: dict) -> str:
    return ", ".join("%d %s" % (len(streams[stream]), label)
                     for stream, label in STREAM_COUNTS)


def _settings(config: dict, ctx) -> extract.Settings:
    return extract.Settings(
        salt=ctx.salt(), account=config["account"],
        hostname=socket.gethostname(),
        machine_label=config["machine_label"],
        round_ts_to_hour=config["round_ts_to_hour"],
        hash_agent_types=config["hash_agent_types"],
        hash_names=config["hash_names"],
    )


def _print_preview(streams: dict) -> None:
    rows = streams["usage"]
    print("dry run: %d new rows, nothing written" % len(rows))
    for stream, label in STREAM_COUNTS:
        print("%s: %d" % (label, len(streams[stream])))
    days = {}
    for row in rows:
        days[row["ts"][:10]] = days.get(row["ts"][:10], 0) + 1
    print("rows per day:")
    for day in sorted(days):
        print("  %s: %d" % (day, days[day]))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=guard.COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows[:SAMPLE_ROWS])
    print("sample rows:")
    print(buffer.getvalue(), end="")


def cmd_sync(args, ctx) -> int:
    print(ctx.sink(ctx.config()).sync(ctx.today))
    return 0


def cmd_run(args, ctx) -> int:
    code = cmd_collect(argparse.Namespace(dry_run=False), ctx)
    if code:
        return code
    check_code = _scheduled_crosscheck(ctx)
    sync_code = cmd_sync(args, ctx)
    return check_code or sync_code


def _scheduled_crosscheck(ctx) -> int:
    config = ctx.config()
    settings = config["crosscheck"]
    if not settings.get("enabled", True):
        return 0
    if not shutil.which(settings.get("command", "ccusage"), path=ctx.environ.get("PATH")):
        return 0
    state = State.load(ctx.state_path)
    if settings.get("period", "week") == "month":
        return _monthly_crosscheck(config, state, ctx)
    return _weekly_crosscheck(config, state, ctx)


def _monthly_crosscheck(config: dict, state: State, ctx) -> int:
    month = periods.previous_month(ctx.today)
    if state.crosscheck_month == month:
        return 0
    code = _crosscheck(argparse.Namespace(month=month, week=None), config, ctx)
    if code == 0:
        state.crosscheck_month = month
        state.save(ctx.state_path)
    return code


def _weekly_crosscheck(config: dict, state: State, ctx) -> int:
    due, skipped = periods.due_weeks(state.crosscheck_week, ctx.today)
    for week in skipped:
        print("skipped cross-check for %s: it started over %d days ago, "
              "so its transcripts may be gone" % (week, periods.MAX_AGE_DAYS))
    for week in due:
        code = _crosscheck(argparse.Namespace(month=None, week=week), config, ctx)
        if code:
            return code
        state.crosscheck_week = week
        state.save(ctx.state_path)
    return 0


def cmd_crosscheck(args, ctx) -> int:
    return _crosscheck(args, ctx.config(), ctx)


def _crosscheck(args, config: dict, ctx) -> int:
    settings = _settings(config, ctx)
    return crosscheck.cmd_crosscheck(
        args, config, settings.machine, settings.salt,
        environ=dict(ctx.environ), today=ctx.today,
    )


def cmd_compare(args, ctx) -> int:
    return crosscheck.cmd_compare(args, ctx.optional_config())


def cmd_analyze(args, ctx) -> int:
    return analyze.cmd_analyze(args, ctx.optional_config())


def cmd_report(args, ctx) -> int:
    return report.cmd_report(args, ctx.optional_config())


def cmd_doctor(args, ctx) -> int:
    results = Doctor(ctx.config_path, ctx.environ).run()
    for level, name, detail in results:
        print("%-4s %s: %s" % (level, name, detail))
    return 1 if any(level == "FAIL" for level, _n, _d in results) else 0


def cmd_status(args, ctx) -> int:
    state = State.load(ctx.state_path)
    print("last run: %s" % (state.last_run or "never"))
    print("rows collected: %d last run, %d total" % (state.last_rows, state.total_rows))
    print("files tracked: %d" % state.files_tracked)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-usage",
        description="Collect number-only Claude Code usage rows.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="config file path")
    parser.add_argument("--debug", action="store_true",
                        help="show the full traceback of an unexpected error")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True
    _add_init(commands)
    collect = commands.add_parser("collect", help="extract new rows and write them")
    collect.add_argument("--dry-run", action="store_true", help="preview, write nothing")
    commands.add_parser("sync", help="pull, commit and push a git destination")
    commands.add_parser("run", help="collect, weekly cross-check, then sync")
    _add_research_commands(commands)
    commands.add_parser("doctor", help="check the install (read only)")
    commands.add_parser("status", help="show the last run")
    return parser


def _add_init(commands) -> None:
    init = commands.add_parser("init", help="write config and salt")
    init.add_argument("--account", required=True, help="account label, e.g. work")
    init.add_argument("--dest-type", choices=("folder", "git"), default="folder")
    init.add_argument("--dest", help="destination folder (default ~/usage-data)")
    init.add_argument("--schedule", help="daily run time HH:MM (default 18:00)")
    init.add_argument("--force", action="store_true", help="replace existing config")


def _add_research_commands(commands) -> None:
    check = commands.add_parser("crosscheck", help="store ccusage totals for a week or month")
    check.add_argument(
        "--week", help="YYYY-Www (default: last completed crosscheck.period)")
    check.add_argument("--month", help="YYYY-MM")
    compare = commands.add_parser("compare", help="compare rows with ccusage totals")
    compare.add_argument("--store", help="store folder (default: config destination)")
    compare.add_argument("--week", help="YYYY-Www (default: every checked period)")
    compare.add_argument("--month", help="YYYY-MM (default: every checked period)")
    compare.add_argument("--tolerance", type=float, default=0.02)
    analyze_parser = commands.add_parser("analyze", help="text usage report")
    analyze_parser.add_argument("--store", help="store folder (default: config destination)")
    analyze_parser.add_argument("--since", help="YYYY-MM-DD")
    analyze_parser.add_argument("--account")
    report_parser = commands.add_parser("report", help="static HTML usage report")
    report_parser.add_argument("--store", help="store folder (default: config destination)")
    report_parser.add_argument("--out", default="report.html")
    report_parser.add_argument("--since", help="YYYY-MM-DD")
    report_parser.add_argument("--svg", help="also write the daily chart as a standalone SVG")


HANDLERS = {
    "init": cmd_init, "collect": cmd_collect, "sync": cmd_sync,
    "run": cmd_run, "crosscheck": cmd_crosscheck, "compare": cmd_compare,
    "analyze": cmd_analyze, "report": cmd_report, "doctor": cmd_doctor,
    "status": cmd_status,
}
EXPECTED_ERRORS = (config_module.ConfigError, SaltError, StateError, SyncError)


def main(argv=None, environ=None, now=None) -> int:
    environ = os.environ if environ is None else environ
    args = build_parser().parse_args(argv)
    ctx = Context(
        config_module.resolve_config_path(args.config, environ), environ,
        now or datetime.now(timezone.utc),
    )
    try:
        return HANDLERS[args.command](args, ctx)
    except GuardError as error:
        _print_guard_error(error)
    except EXPECTED_ERRORS as error:
        print("error: %s" % error, file=sys.stderr)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        print("error: could not read or write a file (%s)" % reason, file=sys.stderr)
    except Exception as error:
        if args.debug:
            raise
        print("error: unexpected %s, run again with --debug for details"
              % type(error).__name__, file=sys.stderr)
    return 1


def _print_guard_error(error: GuardError) -> None:
    for problem in error.problems:
        print("guard: %s" % problem, file=sys.stderr)
    for path in error.files:
        print("guard: rejected rows came from %s" % path, file=sys.stderr)
    print("error: %s" % error, file=sys.stderr)
