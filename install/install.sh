#!/bin/sh
# Install or remove the claude-usage wrapper and its daily launchd agent.
# Safe to run again: every step replaces what an earlier run created.
# No sudo needed: everything lives under $HOME.
# CLAUDE_USAGE_SKIP_LAUNCHCTL=1 writes or removes the plist without
# loading or unloading it (for tests and dry runs).
set -eu

LABEL="io.github.claude-usage-collector"
REPO=$(cd "$(dirname "$0")/.." && pwd)
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/claude-usage"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-usage-collector.log"
CONFIG="${CLAUDE_USAGE_CONFIG:-$HOME/.config/claude-usage-collector/config.json}"

usage() {
    echo "usage: install.sh [--schedule HH:MM] [--python /abs/python3] [--uninstall]"
}

fail() {
    echo "install.sh: $1" >&2
    exit 1
}

is_mac() {
    [ "$(uname -s)" = "Darwin" ]
}

valid_schedule() {
    echo "$1" | grep -Eq '^([01][0-9]|2[0-3]):[0-5][0-9]$'
}

resolve_python() {
    if [ -n "$PYTHON" ]; then
        echo "$PYTHON"
    elif [ -x /usr/bin/python3 ]; then
        echo /usr/bin/python3
    else
        command -v python3 || true
    fi
}

config_schedule() {
    [ -f "$CONFIG" ] || return 0
    "$1" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("schedule", ""))' \
        "$CONFIG" 2>/dev/null || true
}

skip_launchctl() {
    [ "${CLAUDE_USAGE_SKIP_LAUNCHCTL:-}" = "1" ]
}

# XML-escape a value, then escape it again for the sed replacement.
plist_value() {
    printf '%s' "$1" \
        | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
        | sed 's/[&|\\]/\\&/g'
}

render_plist() {
    hour=${SCHEDULE%%:*}
    hour=${hour#0}
    minute=${SCHEDULE##*:}
    minute=${minute#0}
    sed -e "s|__PYTHON__|$(plist_value "$1")|g" \
        -e "s|__REPO__|$(plist_value "$REPO")|g" \
        -e "s|__PATH__|$(plist_value "$PATH")|g" \
        -e "s|__CONFIG__|$(plist_value "$CONFIG")|g" \
        -e "s|__LOG__|$(plist_value "$LOG")|g" \
        -e "s|__HOUR__|$hour|g" \
        -e "s|__MINUTE__|$minute|g" \
        "$REPO/install/launchd.plist.template"
}

unload_agent() {
    skip_launchctl && return 0
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
}

install_agent() {
    mkdir -p "$AGENTS" "$(dirname "$LOG")"
    trap 'rm -f "$PLIST.tmp"' EXIT
    render_plist "$1" > "$PLIST.tmp"
    mv "$PLIST.tmp" "$PLIST"
    if skip_launchctl; then
        echo "wrote $PLIST, not loaded (CLAUDE_USAGE_SKIP_LAUNCHCTL=1)"
        return 0
    fi
    unload_agent
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "launchd agent loaded, runs daily at $SCHEDULE"
}

do_install() {
    python=$(resolve_python)
    case "$python" in
        /*) ;;
        *) fail "no absolute python3 path found, pass --python /path/to/python3" ;;
    esac
    [ -n "$SCHEDULE" ] || SCHEDULE=$(config_schedule "$python")
    [ -n "$SCHEDULE" ] || SCHEDULE="18:00"
    valid_schedule "$SCHEDULE" || fail "schedule must be HH:MM in 24 hour time"
    mkdir -p "$BIN_DIR"
    ln -sfn "$REPO/bin/claude-usage" "$LINK"
    echo "linked $LINK"
    if is_mac; then
        install_agent "$python"
    else
        echo "not macOS: schedule 'claude-usage run' with cron or systemd instead"
    fi
}

do_uninstall() {
    if is_mac; then
        unload_agent
        if [ -e "$PLIST" ]; then
            rm -f "$PLIST"
            echo "removed the launchd agent"
        fi
        rm -f "$PLIST.tmp"
    fi
    if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$REPO/bin/claude-usage" ]; then
        rm -f "$LINK"
        echo "removed the wrapper link $LINK"
    elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
        echo "kept $LINK: it does not point to this repo"
    fi
    echo "kept your config, salt, state and log; delete them by hand if you want"
}

SCHEDULE=""
PYTHON=""
ACTION="install"
while [ $# -gt 0 ]; do
    case "$1" in
        --schedule) [ $# -ge 2 ] || fail "--schedule needs HH:MM"; SCHEDULE=$2; shift 2 ;;
        --python) [ $# -ge 2 ] || fail "--python needs a path"; PYTHON=$2; shift 2 ;;
        --uninstall) ACTION="uninstall"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ -n "$SCHEDULE" ]; then
    valid_schedule "$SCHEDULE" || fail "schedule must be HH:MM in 24 hour time"
fi

if [ "$ACTION" = "uninstall" ]; then
    do_uninstall
else
    do_install
fi
