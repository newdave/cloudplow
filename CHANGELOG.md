# Changelog

All notable changes to this fork are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The fork is not versioned; entries are grouped by the
date they reached `master`.

Changes are relative to [l3uddz/cloudplow](https://github.com/l3uddz/cloudplow) at commit `584263a`.

## Unreleased

### Added

- **Load-based rate limiting.** A per-uploader `rate_script` runs every `rate_script_interval` seconds during an
  upload; a changed `--bwlimit` is applied to the running rclone immediately through its remote control.
- **Rclone overrides.** Scripts can write `overrides/<remote>.json` next to `config.json` to tune `--bwlimit`,
  `--transfers`, `--checkers`, `--tpslimit`, `--multi-thread-streams` or `--buffer-size` for the next upload.
- **Post script environment.** `post_script` receives `CLOUDPLOW_REMOTE`, `CLOUDPLOW_STATUS`, `CLOUDPLOW_REASON`,
  `CLOUDPLOW_UPLOAD_FOLDER`, `CLOUDPLOW_UPLOAD_REMOTE`, `CLOUDPLOW_CONFIG_DIR`, `CLOUDPLOW_OVERRIDES_FILE` and
  `CLOUDPLOW_DRY_RUN`.
- **Bundled scripts** in `scripts/post_script/`: a `post.d/` dispatcher, load-based tuning (local and NAS load over
  SSH), healthchecks.io / Uptime Kuma pings, and permission fixes for uploaded files.
- **`verified_move`** remote option: upload with `rclone copy`, then remove each local file only if it is unchanged
  since it was listed. Files replaced mid-upload are kept; files deleted or renamed mid-upload have their stale
  destination copy removed. Makes uploading into a mergerfs branch safe.
- **`require_mounts`** remote option: skip the upload while any listed mount point is not mounted.
- **qBittorrent support**: alternative speed limits ("turtle mode") are switched on during uploads and back off
  afterwards, unless they were already on. Interrupted uploads are cleaned up at the next start.
- **`opened_files_sudo`** uploader option: run the open-file check (`lsof`) through passwordless `sudo` so files held
  open by mergerfs (root) are detected.
- **Log redaction** of URL credentials, Plex tokens and API keys, tracebacks included.
- A `unittest` suite, CI (tests and coverage on Python 3.8, ShellCheck, actionlint, a single `CI gate` check), and
  the usual project files: security policy, code of conduct, issue and pull request templates.
- Repository workflows: weekly upstream sync, pull request labelling, label sync, stale issues, Dependabot.
- **mise tasks** for development (`setup`, `test`, `coverage`, `lint`, `check`, `ci:local`) and operations
  (`service:status`, `service:restart`, `logs`, `config:check`, `saltbox:install`, `nas:install-key`,
  `nas:check-key`, `tune:show`, `upstream:check`, `github:settings`).
- Tests for Saltbox compatibility: its generated `config.json` and `update_config` work unchanged.

### Changed

- **Without `max_size_gb`, uploads run every `check_interval`.** Use rclone's `--min-age` in `rclone_extras` to hold
  back files that are still being written. With `max_size_gb` (as Saltbox generates), uploads still wait for the
  folder to reach that size, as upstream does.
- Plex throttling and the rate script share one limit: the slower wins, and when Plex streams stop the upload returns
  to the rate script's limit instead of unlimited.
- `post_script` (from this fork's earlier `post_script` commits) runs after each remote's upload, with its exit code
  logged, instead of once per upload run with the last remote's settings.
- External commands are built as argument lists; no shell strings.
- `cloudplow.py` was restructured around a `Cloudplow` class; rclone argument building and trigger tracking are
  shared by the uploader, mover and syncer.
- Locks use `fcntl.flock` instead of the unmaintained `lockfile` package.
- Logging is set up in `utils/logs.py`; every module logs under its own name, and startup messages are kept until the
  log file is known.
- Requires Python 3.8 or later.

### Fixed

- The folder size (`max_size_gb`) is measured with `du` as an argument list instead of a shell pipeline, so
  `size_excludes` patterns with quotes or spaces work.

- Open-file excludes are escaped with rclone's glob syntax, so files with `[]` or `{}` in their names (all
  Radarr/Sonarr names) are actually excluded.
- Remote (non-local) syncers crashed on schedule.
- The Plex stream monitor logged a `Connection refused` traceback and gave up when an upload had nothing to move
  (rclone never opened its remote control). It now waits for rclone's remote control while the upload runs and
  exits quietly when the upload ends first.
- Trigger-aborted uploads always suspended for 25 hours, ignoring the trigger's `sleep`.
- The mover reported success regardless of rclone's exit code.
- The hidden cleaner never found files, over-stripped `_HIDDEN~` names and passed `dry_run` as the Plex argument.
- Service account unbans were never persisted; the suspension check was passed a dict instead of the remote name.
- Process output lost trailing lines and crashed on non-UTF-8 file names.
- The throttle speed picked the nearest key instead of the nearest lower one.
- The Scaleway error handler crashed, and a failed config copy-back could truncate `rclone.conf`.
- NZBGet changed the global socket timeout; Sabnzbd paused twice, printed instead of logging and had no timeout.
- The Plex token was logged on errors, and TLS verification was disabled.
- Apprise never reported a successful send.
- Throttle rates other than whole megabytes (such as `512k`) crashed the throttle check.
- `post_script`'s default config key was `postscript`, which the code never read.
