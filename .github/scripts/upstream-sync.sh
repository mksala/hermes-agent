#!/usr/bin/env bash
# Sync the fork's `main` with NousResearch/hermes-agent and report whether the
# local Railway patches on `railway` still rebase cleanly. Notify via a GitHub
# issue. Never deploys and never mutates `railway` on the fork — the deploy
# stays a deliberate, human-triggered step. Run by .github/workflows/upstream-sync.yml.
set -euo pipefail

UPSTREAM_URL="https://github.com/NousResearch/hermes-agent.git"
LABEL="upstream-sync"

# Commit identity must stay 10102-safe (no real name / personal email).
git config user.name "mk"
git config user.email "mk@10102.io"

git remote add upstream "$UPSTREAM_URL" 2>/dev/null || git remote set-url upstream "$UPSTREAM_URL"
git fetch --no-tags --quiet upstream main
git fetch --no-tags --quiet origin main

upstream_sha="$(git rev-parse upstream/main)"
fork_sha="$(git rev-parse origin/main)"

if [ "$upstream_sha" = "$fork_sha" ]; then
  echo "Fork main is already at upstream ($upstream_sha). Nothing to do."
  exit 0
fi

behind="$(git rev-list --count "${fork_sha}..${upstream_sha}")"
short="${upstream_sha:0:7}"
echo "Upstream is ${behind} commit(s) ahead -> ${short}. Fast-forwarding fork main."

# `main` is a pure mirror of upstream — fast-forward it (no merge commit).
git push origin "refs/remotes/upstream/main:refs/heads/main"

# Probe (in a throwaway branch) whether the railway patches still apply on the
# new main. The fork's `railway` branch is left untouched.
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
Upstream **NousResearch/hermes-agent** advanced by **${behind}** commit(s) to \`${short}\`.

The fork's \`main\` was fast-forwarded. A test rebase of \`railway\` onto the new \`main\` applied **cleanly** :white_check_mark:.

**To ship the update:**
\`\`\`sh
cd ~/DevMac/hermes
git fetch fork
git checkout main && git reset --hard fork/main
git checkout railway && git rebase main && git push -f fork railway
railway up --service hermes-agent --detach
\`\`\`
\`HERMES_DASHBOARD_INSECURE=1\` must remain set on the Railway service.
EOF
else
cat > "$body_file" <<EOF
Upstream **NousResearch/hermes-agent** advanced by **${behind}** commit(s) to \`${short}\`.

The fork's \`main\` was fast-forwarded, but the \`railway\` rebase **conflicts** :warning: and needs manual resolution.

Conflicting files: \`${conflicts}\`

**Resolve locally:**
\`\`\`sh
cd ~/DevMac/hermes
git fetch fork
git checkout main && git reset --hard fork/main
git checkout railway && git rebase main
# fix the conflicts, then:
git rebase --continue && git push -f fork railway
railway up --service hermes-agent --detach
\`\`\`
EOF
fi

gh label create "$LABEL" --color FBCA04 --description "Upstream hermes-agent has new commits" >/dev/null 2>&1 || true
existing="$(gh issue list --label "$LABEL" --state open --json number --jq '.[0].number // empty')"
if [ -n "$existing" ]; then
  echo "Commenting on existing issue #${existing}."
  gh issue comment "$existing" --body-file "$body_file"
else
  echo "Opening a new tracking issue."
  gh issue create --title "Upstream sync: hermes-agent has new commits" --label "$LABEL" --body-file "$body_file"
fi
