#!/usr/bin/env bash
# cloudplow post_script dispatcher: runs every executable in post.d/ in name order.
#
# Settings are read from post_script.conf next to cloudplow's config.json (or $CLOUDPLOW_POST_CONF).
# A failing part is logged and the rest still run; the exit code is non-zero if any part failed.
set -u

here=$(dirname "$(readlink -f "$0")")
conf=${CLOUDPLOW_POST_CONF:-${CLOUDPLOW_CONFIG_DIR:-$here}/post_script.conf}
if [ -r "$conf" ]; then
    set -a
    # shellcheck source=/dev/null
    . "$conf"
    set +a
fi
export CLOUDPLOW_POST_CONF_LOADED=1

rc=0
for part in "$here"/post.d/*; do
    [ -f "$part" ] && [ -x "$part" ] || continue
    "$part" || { echo "post.d/$(basename "$part") failed with exit code $?"; rc=1; }
done
exit "$rc"
