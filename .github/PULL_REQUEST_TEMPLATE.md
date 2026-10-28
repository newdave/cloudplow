## What and why

<!-- What does this change, and what problem does it solve? Link the issue: "Fixes #123". -->

## How it was tested

<!-- Tests added or updated, and anything you ran by hand (configuration, rclone version, destination type). -->

## Checklist

- [ ] `mise run check` passes (or each of the three checks below)
- [ ] `python3 -m unittest discover -s tests` passes
- [ ] New or changed code has tests, so coverage does not drop (`python3 -m coverage report`)
- [ ] `shellcheck scripts/post_script/post.sh scripts/post_script/post.d/*` passes (if scripts changed)
- [ ] README, `config.json.sample` and the `Unreleased` section of `CHANGELOG.md` are updated (if behaviour or settings changed)
- [ ] No secrets in code, tests or logs
