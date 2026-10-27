#!/usr/bin/env bash
# Bring upstream changes into this fork for review.
#
# If the upstream branch has commits this repository lacks, merge them into $SYNC_BRANCH and open (or refresh) a pull
# request. If the merge conflicts, open (or refresh) an issue listing the commits instead, since resolving it needs a
# person. When the fork is up to date, any open sync issue is closed. Needs git and an authenticated gh.
set -euo pipefail

UPSTREAM_URL=${UPSTREAM_URL:-https://github.com/l3uddz/cloudplow}
UPSTREAM_BRANCH=${UPSTREAM_BRANCH:-master}
BASE_BRANCH=${BASE_BRANCH:-master}
SYNC_BRANCH=${SYNC_BRANCH:-upstream-sync}
LABEL=Upstream
ISSUE_TITLE="Upstream changes need a manual merge"
work=${RUNNER_TEMP:-$(mktemp -d)}

git remote remove upstream 2>/dev/null || true
git remote add upstream "$UPSTREAM_URL.git"
git fetch --no-tags upstream "$UPSTREAM_BRANCH"
upstream_ref="upstream/$UPSTREAM_BRANCH"
git checkout --quiet "$BASE_BRANCH"

open_issue=$(gh issue list --state open --label "$LABEL" --search "in:title \"$ISSUE_TITLE\"" \
                 --json number --jq '.[0].number // empty')

count=$(git rev-list --count "HEAD..$upstream_ref")
if [ "$count" -eq 0 ]; then
    echo "Up to date with $UPSTREAM_URL ($UPSTREAM_BRANCH)."
    if [ -n "$open_issue" ]; then
        gh issue close "$open_issue" --comment "$BASE_BRANCH now contains every upstream commit."
    fi
    exit 0
fi

{
    echo "$count new commit(s) on [$UPSTREAM_BRANCH]($UPSTREAM_URL/commits/$UPSTREAM_BRANCH):"
    echo
    git log --reverse --format="- [\`%h\`]($UPSTREAM_URL/commit/%H) %s (%an, %as)" "HEAD..$upstream_ref"
    echo
} > "$work/commits.md"

# make sure the label exists even before the Labels workflow has run
gh label create "$LABEL" --color fbca04 --description "Changes from l3uddz/cloudplow" --force >/dev/null

git switch --quiet -C "$SYNC_BRANCH"
if git merge --no-ff --no-edit -m "Merge $UPSTREAM_BRANCH from $UPSTREAM_URL" "$upstream_ref"; then
    git push --force origin "$SYNC_BRANCH"
    {
        cat "$work/commits.md"
        echo "This branch is rebuilt by the Upstream Sync workflow, so do not push to it."
        echo "Pull requests opened by GitHub Actions do not start CI: close and reopen this one to run the checks."
    } > "$work/body.md"
    open_pr=$(gh pr list --state open --head "$SYNC_BRANCH" --json number --jq '.[0].number // empty')
    if [ -n "$open_pr" ]; then
        gh pr edit "$open_pr" --body-file "$work/body.md"
        echo "Updated pull request #$open_pr."
    else
        gh pr create --base "$BASE_BRANCH" --head "$SYNC_BRANCH" --label "$LABEL" \
            --title "Merge upstream changes from l3uddz/cloudplow" --body-file "$work/body.md"
    fi
    if [ -n "$open_issue" ]; then
        gh issue close "$open_issue" --comment "Upstream now merges cleanly; see the open pull request."
    fi
else
    conflicts=$(git diff --name-only --diff-filter=U)
    git merge --abort
    {
        cat "$work/commits.md"
        echo "Merging them into \`$BASE_BRANCH\` conflicts in:"
        echo
        # shellcheck disable=SC2016  # the backticks are Markdown, not command substitution
        sed 's/^/- `/; s/$/`/' <<<"$conflicts"
        echo
        echo "To merge by hand:"
        echo
        echo '```'
        echo "git fetch $UPSTREAM_URL $UPSTREAM_BRANCH"
        echo "git merge FETCH_HEAD"
        echo '```'
    } > "$work/body.md"
    if [ -n "$open_issue" ]; then
        gh issue edit "$open_issue" --body-file "$work/body.md"
        echo "Updated issue #$open_issue."
    else
        gh issue create --title "$ISSUE_TITLE" --label "$LABEL" --body-file "$work/body.md"
    fi
fi
