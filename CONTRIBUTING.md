# Contributing

Thanks for helping improve Cloudplow. Bug reports, fixes and features are all welcome.

## Before you start

- Search the [existing issues](https://github.com/newdave/cloudplow/issues) first.
- For anything bigger than a small fix, open an issue to discuss it before writing the code.
- Questions about upstream behaviour that this fork has not changed are best asked at
  [l3uddz/cloudplow](https://github.com/l3uddz/cloudplow).

## Development setup

```
git clone https://github.com/<your_github_username>/cloudplow
cd cloudplow
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

or, with [mise](https://mise.jdx.dev/), `mise run setup`. `mise tasks` lists the other tasks; the
[README](README.md#mise-tasks) describes them.

Cloudplow supports Python 3.8 and later, so avoid syntax and standard library features newer than 3.8.

## Making a change

1. Create a branch off `master`:

   ```
   git checkout -b fix/short-description master
   ```

2. Make your change, with tests in `tests/` for new behaviour or fixed bugs.

3. Run the checks that CI runs. With mise, one command does all of them, including the coverage comparison with
   `master`:

   ```
   mise run check
   ```

   Without mise:

   ```
   venv/bin/python3 -m coverage run -m unittest discover -s tests
   venv/bin/python3 -m coverage report
   shellcheck scripts/post_script/post.sh scripts/post_script/post.d/* .github/scripts/*.sh
   ```

   Install `rclone` and run the tests as a regular user (not root), otherwise the verified-move tests are skipped.
   CI runs the tests on Python 3.8 only, the version Saltbox installs; `mise run ci:local` runs the same job in
   Docker. CI also fails a pull request that lowers total test coverage.

4. Update the documentation: the [README](README.md) for new or changed settings, `config.json.sample` for new
   config keys, and the `Unreleased` section of the [changelog](CHANGELOG.md).

5. Keep `master` current while you work:

   ```
   git pull --rebase https://github.com/newdave/cloudplow master
   ```

6. Push your branch and open a pull request against `master`.

## Repository settings (maintainers)

CI and the workflows rely on three GitHub settings: the `CI gate` check required on `master` (as a ruleset that
repository admins may bypass, so they can still push directly), GitHub Actions being allowed to create pull requests
(for the Upstream Sync workflow), and private vulnerability reporting (linked from `SECURITY.md`). With
[mise](https://mise.jdx.dev/) and an authenticated `gh`:

```
mise run github:settings status
mise run github:settings enable --dry-run   # show the changes first
mise run github:settings enable             # add --strict to require the gate for admins too
mise run github:settings disable
```

## Guidelines

- Match the surrounding code: type hints, `logging.getLogger(__name__)`, and external commands built as argument lists
  (never shell strings).
- Never log secrets. The log formatter redacts common token and credential patterns, but do not rely on it.
- Changes that delete or move user files need a test showing what happens when a file changes during the operation.
- Write commit messages that explain why the change was made, not only what changed.
