#!/usr/bin/env bash
# Track NousResearch/hermes-agent and report whether the local Railway patches
# on `railway` still rebase cleanly onto the latest upstream main. Notify via a
# GitHub issue. Never deploys and never mutates `railway`/`main` on the fork.
#
# Note: CI deliberately does NOT push the `main` mirror. GitHub blocks the
# default GITHUB_TOKEN from pushing changes under .github/workflows/, which
# every upstream sync touches. The `main` mirror is instead refreshed during
# the manual update (the local mksala token has `workflow` scope). The rebase
# here is probed directly against upstream/main, so the mirror isn't needed.
#
# Run by .github/workflows/upstream-sync.yml. In CI: origin = the fork,
# upstream = NousResearch (added below).
set -euo pipefail

UPSTREAM_URL="https://github.com/NousResearch/hermes-agent.git"
LABEL="upstream-sync"

git config user.name "mk"
git config user.email "mk@10102.io"

git remote add upstream "$UPSTREAM_URL" 2>/dev/null || git remote set-url upstream "$UPSTREAM_URL"
git fetch --no-tags --quiet upstream main

upstream_sha="$(git rev-parse upstream/main)"
base="$(git merge-base railway upstream/main)"

if [ "$base" = "$upstream_sha" ]; then
  echo "railway already contains every upstream commit ($upstream_sha). Nothing to do."
  exit 0
fi

behind="$(git rev-list --count "${base}..${upstream_sha}")"
short="${upstream_sha:0:7}"
echo "railway is behind upstream by ${behind} commit(s); upstream main at ${short}."

# Probe (in a throwaway branch) whether the railway patches still apply on the
# latest upstream main. The fork's `railway` branch is left untouched.
git checkout -B _rebase_probe railway >/dev/null 2>&1
if git rebase upstream/main >/dev/null 2>&1; then
  result="clean"
  conflicts=""
else
  result="conflict"
  conflicts="$(git diff --name-only --diff-filter=U | paste -sd ', ' -)"
  git rebase --abort || true
fi
git checkout railway >/dev/null 2>&1
git branch -D _rebase_probe >/dev/null 2>&1 || true
echo "Rebase probe: ${result}${conflicts:+ (conflicts: ${conflicts})}"

body_file="$(mktemp)"
if [ "$result" = "clean" ]; then
cat > "$body_file" <<EOF
Upstream **NousResearch/hermes-agent** is **${behind}** commit(s) ahead of the railway patches (upstream main at \`${short}\`).

A test rebase of \`railway\` onto the latest upstream main applied **cleanly** :white_check_mark:.

**To ship the update** (locally; \`origin\` = upstream, \`fork\` = mksala/hermes-agent):
\`\`\`sh
cd ~/DevMac/hermes
git fetch origin
git checkout railway && git rebase origin/main && git push -f ghfork railway
git branch -f main origin/main && git push ghfork main   # refresh the mirror
railway up --service hermes-agent --detach
\`\`\`
\`HERMES_DASHBOARD_INSECURE=1\` must remain set on the Railway service.
EOF
else
cat > "$body_file" <<EOF
Upstream **NousResearch/hermes-agent** is **${behind}** commit(s) ahead of the railway patches (upstream main at \`${short}\`).

A test rebase of \`railway\` onto the latest upstream main **conflicts** :warning: and needs manual resolution.

Conflicting files: \`${conflicts}\`

**Resolve locally** (\`origin\` = upstream, \`fork\` = mksala/hermes-agent):
\`\`\`sh
cd ~/DevMac/hermes
git fetch origin
git checkout railway && git rebase origin/main
# fix the conflicts, then:
git rebase --continue && git push -f ghfork railway
git branch -f main origin/main && git push ghfork main   # refresh the mirror
railway up --service hermes-agent --detach
\`\`\`
EOF
fi

# Use the REST API (gh api), not gh issue create/list: the latter go through
# GraphQL, which fine-grained PATs routinely get "Resource not accessible" on
# even with Issues:write. REST works with the same scope.
# Dedup by exact title (REST issues list includes PRs, so filter those out).
repo="${GITHUB_REPOSITORY:-mksala/hermes-agent}"
export TITLE="Upstream sync: hermes-agent has new commits"
body="$(cat "$body_file")"
existing="$(gh api "repos/${repo}/issues?state=open&per_page=100" \
  --jq '.[] | select(has("pull_request")|not) | select(.title==env.TITLE) | .number' | head -1)"
if [ -n "$existing" ]; then
  echo "Commenting on existing issue #${existing}."
  gh api -X POST "repos/${repo}/issues/${existing}/comments" -f body="$body" >/dev/null
else
  echo "Opening a new tracking issue."
  gh api -X POST "repos/${repo}/issues" -f title="$TITLE" -f body="$body" >/dev/null
fi
