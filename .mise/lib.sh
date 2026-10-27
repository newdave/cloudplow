# Shared by the tasks in .mise/tasks/ (sourced, not run).
# shellcheck shell=bash

ROOT=${MISE_PROJECT_ROOT:-$(git rev-parse --show-toplevel)}
cd "$ROOT" || exit 1
PY="$ROOT/venv/bin/python3"
SCRATCH_PREFIX=cloudplow-task

die() { echo "error: $*" >&2; exit 1; }
note() { echo "==> $*"; }

need_venv() {
    [[ -x $PY ]] || die "no virtual environment at venv/; run: mise run setup"
}

# Files the linters check, the same list CI uses
shell_files() {
    printf '%s\n' scripts/post_script/post.sh scripts/post_script/post.d/* .github/scripts/*.sh
    find .mise -type f | sort
}

# The systemd unit cloudplow runs as: Saltbox's name, else the one in systemd/
service_name() {
    local unit
    for unit in saltbox_managed_cloudplow cloudplow; do
        if systemctl list-unit-files "$unit.service" --no-legend 2>/dev/null | grep -q .; then
            echo "$unit"
            return
        fi
    done
    return 1
}

# True while an upload holds cloudplow's upload lock (probed the same way cloudplow does)
upload_running() {
    [[ -e $ROOT/locks/upload.flock ]] || return 1
    ! flock --nonblock --exclusive "$ROOT/locks/upload.flock" true 2>/dev/null
}

# Source post_script.conf (NAS_SSH, TUNE_TIERS, ...) if there is one
load_post_conf() {
    local conf=${CLOUDPLOW_POST_CONF:-$ROOT/post_script.conf}
    if [[ -r $conf ]]; then
        set -a
        # shellcheck source=/dev/null
        . "$conf"
        set +a
    fi
}

# Sets SCRATCH to a temporary directory removed when the task exits (call directly, not in $(...))
make_scratch() {
    SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/$SCRATCH_PREFIX.XXXXXX")
    trap 'rm -rf "$SCRATCH"' EXIT
}
