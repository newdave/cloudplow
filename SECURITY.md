# Security Policy

## Supported versions

Only the latest commit on `master` is supported. Please update before reporting.

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report them privately through GitHub instead:
[Report a vulnerability](https://github.com/newdave/cloudplow/security/advisories/new).

Include what an attacker could do, the steps or configuration needed to reproduce it, and the commit you tested.
You should get a response within a week. Once a fix is available, the advisory is published with credit to you
unless you ask otherwise.

## Scope

Cloudplow runs with the permissions of its user and reads `config.json`, `rclone.conf` and service account keys.
Issues where Cloudplow exposes those secrets (for example in logs or notifications), runs commands it should not, or
deletes or overwrites files outside the configured folders are in scope. Vulnerabilities in rclone itself should be
reported to the [rclone project](https://github.com/rclone/rclone/security).
