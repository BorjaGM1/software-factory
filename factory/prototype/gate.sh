#!/usr/bin/env bash
# gate.sh BRANCH [BASE]  -- council protocol steps 1-4, 7, 8 for one branch, on a throwaway worktree.
set -u
repo="${TARGET_REPO:-$HOME/projects/renfe-cli-lab}"; V="$(cd "$(dirname "$0")" && pwd)"; b="$1"; base="${2:-main}"
wt="$V/wt-$(echo "$b" | tr / -)"; git -C $repo worktree remove --force "$wt" 2>/dev/null; git -C $repo worktree add -q --detach "$wt" "$b"
cd "$wt"
echo "### $b (base $base)"
echo "1. commits: $(git log --oneline $base..HEAD | wc -l | tr -d ' ')  diff --check: $(git diff --check $base...HEAD >/dev/null 2>&1 && echo clean || echo ISSUES)  files: $(git diff --name-only $base...HEAD | tr '\n' ' ')"
uv lock --check -q >/dev/null 2>&1 && l=ok || l=FAIL; uv sync -q --locked >/dev/null 2>&1 && s=ok || s=FAIL; echo "2. uv lock --check: $l  uv sync --locked: $s"
if [ "$b" != fix/pin-clock ]; then git -c user.name=gate -c user.email=gate@local merge -q --no-edit fix/pin-clock >/dev/null 2>&1 || { git merge --abort; echo "   (merge with pin-clock conflicted)"; }; fi
echo "3. suite (with #20): $(uv run pytest -q -p no:cacheprovider 2>&1 | tail -1)"
git stash -q 2>/dev/null; cp -R tests /tmp/gate-newtests-$$ && rm -rf tests && git archive $base tests | tar -x
echo "4. main's tests vs branch src: $(uv run pytest -q -p no:cacheprovider 2>&1 | grep -E 'FAILED|passed|failed' | sed 's/ - .*//' | tr '\n' ';')"
rm -rf tests && mv /tmp/gate-newtests-$$ tests
for d in 1 92 730; do echo "7. clock +$d days: $(GATE_SHIFT_DAYS=$d PYTHONPATH=$V uv run pytest -q -p no:cacheprovider -p shift_plugin 2>&1 | tail -1)"; done
PYTHONTZPATH="" uv run --locked --no-dev renfe open MADRI BARCE --date tomorrow --no-browser >/dev/null 2>&1 && t=ok || t=FAIL
echo "8. startup: $(uv run --locked --no-dev renfe --version 2>&1 | tail -1); without system tz data: $t"
cd /; git -C $repo worktree remove --force "$wt"
