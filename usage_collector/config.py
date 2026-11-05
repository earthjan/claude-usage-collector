import copy
import json
import os
import re

from . import guard, hashing

ENV_VAR = "CLAUDE_USAGE_CONFIG"
DEFAULT_PATH = "~/.config/claude-usage-collector/config.json"
SCHEDULE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")

DEFAULTS = {
    "account": None,
    "machine_label": None,
    "sources": ["~/.claude/projects"],
    "destination": None,
    "round_ts_to_hour": False,
    "hash_agent_types": False,
    "hash_names": True,
    "schedule": "18:00",
    "crosscheck": {"command": "ccusage", "enabled": True, "period": "week"},
    "weights": {"input": 1, "cache_read": 0.1, "cache_write": 2, "output": 5},
    "prices": {},
}
GIT_DEFAULTS = {"remote": "origin", "branch": "main", "push": True}
TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")
CROSSCHECK_PERIODS = ("week", "month")


class ConfigError(Exception):
    pass


def resolve_config_path(explicit, environ) -> str:
    path = explicit or environ.get(ENV_VAR) or DEFAULT_PATH
    return os.path.expanduser(path)


def salt_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(config_path), "salt")


def state_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(config_path), "state.json")


def name_map_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(config_path), "names.json")


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def init_config(path, account, dest_type="folder", dest=None,
                schedule=None, force=False) -> dict:
    if not dest:
        raise ConfigError(
            "init needs --dest, the folder for your usage data "
            "(a private place, never this tool's own repo)"
        )
    if os.path.exists(path) and not force:
        raise ConfigError("config already exists, use --force to replace it")
    data = _initial_config(account, dest_type, dest, schedule)
    validate(data)
    _write_private(path, json.dumps(data, indent=2) + "\n")
    hashing.create_salt(salt_path(path))
    return data


def _initial_config(account, dest_type, dest, schedule) -> dict:
    destination = {"type": dest_type, "path": dest}
    if dest_type == "git":
        destination.update(GIT_DEFAULTS)
    data = {key: copy.deepcopy(DEFAULTS[key]) for key in (
        "machine_label", "sources", "round_ts_to_hour",
        "hash_names", "crosscheck",
    )}
    data.update({
        "account": account,
        "destination": destination,
        "schedule": schedule or DEFAULTS["schedule"],
    })
    return data


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(temp, path)


def load_config(path: str) -> dict:
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise ConfigError("no config found, run init first") from None
    except (OSError, ValueError):
        raise ConfigError("config is not valid JSON") from None
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    merged = _with_defaults(data)
    validate(merged)
    return merged


def _with_defaults(data: dict) -> dict:
    merged = copy.deepcopy(DEFAULTS)
    merged.update(data)
    merged["hash_names"] = _hash_names(data)
    destination = merged["destination"]
    if isinstance(destination, dict) and destination.get("type") == "git":
        merged["destination"] = dict(GIT_DEFAULTS, **destination)
    return merged


def _hash_names(data: dict):
    """On unless hash_names is false and the older hash_agent_types is not true.

    An older init wrote hash_agent_types false as its default, not as a
    choice, so false there leaves the decision to hash_names.
    """
    if data.get("hash_agent_types") is True:
        return True
    return data.get("hash_names", True)


def validate(data: dict) -> None:
    if not guard.is_slug(data.get("account")):
        raise ConfigError("account must be a short slug such as work")
    label = data.get("machine_label")
    if label is not None and not guard.is_slug(label):
        raise ConfigError("machine_label must be null or a short slug")
    if not isinstance(data.get("schedule"), str) or not SCHEDULE.fullmatch(data["schedule"]):
        raise ConfigError("schedule must be HH:MM in 24 hour time")
    _validate_sources(data.get("sources"))
    _validate_destination(data.get("destination"))
    for key in ("round_ts_to_hour", "hash_agent_types", "hash_names"):
        if not isinstance(data.get(key, DEFAULTS[key]), bool):
            raise ConfigError("%s must be true or false" % key)
    _validate_crosscheck(data.get("crosscheck", DEFAULTS["crosscheck"]))
    _validate_weights(data.get("weights", DEFAULTS["weights"]))
    _validate_prices(data.get("prices", DEFAULTS["prices"]))


def is_token_number_map(value) -> bool:
    """True for an object mapping token kinds to numbers >= 0."""
    if not isinstance(value, dict):
        return False
    return all(
        key in TOKEN_KEYS and isinstance(number, (int, float))
        and not isinstance(number, bool) and number >= 0
        for key, number in value.items()
    )


def _validate_crosscheck(crosscheck) -> None:
    if not isinstance(crosscheck, dict):
        raise ConfigError("crosscheck must be an object")
    command = crosscheck.get("command", "ccusage")
    if not isinstance(command, str) or not command:
        raise ConfigError("crosscheck command must be a command name")
    if not isinstance(crosscheck.get("enabled", True), bool):
        raise ConfigError("crosscheck enabled must be true or false")
    if crosscheck.get("period", "week") not in CROSSCHECK_PERIODS:
        raise ConfigError("crosscheck period must be week or month")


def _validate_weights(weights) -> None:
    if not is_token_number_map(weights):
        raise ConfigError("weights entries must be token kinds with numbers >= 0")


def _validate_prices(prices) -> None:
    valid = isinstance(prices, dict) and all(
        isinstance(prefix, str) and is_token_number_map(table)
        for prefix, table in prices.items()
    )
    if not valid:
        raise ConfigError(
            "prices must map model prefixes to token kinds with numbers >= 0"
        )


def _validate_sources(sources) -> None:
    valid = isinstance(sources, list) and sources and all(
        isinstance(source, str) and source for source in sources
    )
    if not valid:
        raise ConfigError("sources must be a non-empty list of paths")


def _validate_destination(destination) -> None:
    if destination is None:
        raise ConfigError(
            "destination is required: set it to a private folder or git clone "
            "for your usage data"
        )
    if not isinstance(destination, dict):
        raise ConfigError("destination must be an object")
    if destination.get("type") not in ("folder", "git"):
        raise ConfigError("destination type must be folder or git")
    if not isinstance(destination.get("path"), str) or not destination["path"]:
        raise ConfigError("destination path is required")
