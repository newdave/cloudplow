---
name: Bug Report
about: Report something that is not working as documented
labels: Bug
---

**Describe the bug**
A clear and concise description of what the bug is.

**To reproduce**
The command or schedule that triggers it, and the relevant part of `config.json` (remove tokens, passwords and API keys).

**Expected behaviour**
What you expected to happen.

**Logs**
The relevant part of `cloudplow.log`, ideally with `--loglevel=DEBUG`. Common secrets are redacted automatically, but
check before posting.

**System information**

- Cloudplow commit (`git -C /opt/cloudplow rev-parse --short HEAD`):
- Python version (`python3 --version`):
- Rclone version (`rclone version`):
- Operating system:
- Upload destination (cloud remote, NFS, mergerfs branch, ...):

**Additional context**
Anything else that might help.
