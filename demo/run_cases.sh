#!/usr/bin/env bash
# The demo's final pass: the seven cases, one at a time, with the builder visible and everything recorded.
#
#   demo/run_cases.sh OUT_DIR [FROM_CASE]
#
# Serves the dashboard in presentation mode, records it, runs each case, and leaves in OUT_DIR: dash/ (the
# dashboard recording), casts/ (one per terminal shown), marks.tsv (when each case started and ended), costs.txt,
# rules.txt and the events log.
#
# RESET=1 starts from a clean factory, so what it learns, it learns on camera. It moves the runs aside, DELETES
# the learned rules, and deletes the factory/* branches of the demo target's clone (TARGET_REPO).
set -u
cd "$(dirname "$0")/.."
OUT=$(realpath -m "$1"); FROM=${2:-1}
mkdir -p "$OUT/casts"
export FACTORY_BUILDER_VISIBLE=1 FACTORY_AGENT_MODEL=${FACTORY_AGENT_MODEL:-haiku}
GAP=${GAP:-20}   # seconds between cases, so the paced dashboard catches up before the next one

mark() { printf '%s\t%s\t%s\n' "$(date +%s.%N)" "$1" "$2" >> "$OUT/marks.tsv"; echo "== $(date +%T) case $1 $2"; }
newest_event() { ls -t runs/inbox/*.json 2>/dev/null | head -1; }

TARGET_REPO=${TARGET_REPO:-$HOME/projects/renfe-cli-lab}
if [ "$FROM" = 1 ] && [ "${RESET:-0}" = 1 ]; then
  mkdir -p runs/.before && mv runs/events.jsonl runs/run-* runs/inbox runs/.before/ 2>/dev/null
  rm -f rules/*.json
  git -C "$TARGET_REPO" branch --list 'factory/*' | xargs -r git -C "$TARGET_REPO" branch -D >/dev/null
  git -C "$TARGET_REPO" worktree prune
fi
mkdir -p runs/inbox && touch runs/events.jsonl

setsid python3 dashboard/serve.py --port 8765 > "$OUT/serve.log" 2>&1 < /dev/null &
SERVE=$!
sleep 2
setsid node demo/rec_dash.js "http://127.0.0.1:8765/?present=1" "$OUT/dash" > "$OUT/rec.log" 2>&1 < /dev/null &
REC=$!
sleep 8

case_on() { [ "$1" -ge "$FROM" ]; }

if case_on 1; then
  mark 1 start
  python3 demo/rec_cmd.py "$OUT/casts/1-canaries.cast" --title "Monitorización: canaries contra el gemelo de Renfe" -- \
    python3 -m intake.monitoring --mode shape-drift
  bin/factory run "$(newest_event)" --retries 1
  mark 1 end; sleep "$GAP"
fi
if case_on 2; then
  mark 2 start
  before=$(newest_event)
  python3 demo/show_feedback.py intake/feedback/samples/05-direct-only.json --hold 4
  evt=$(newest_event)
  [ "$evt" != "$before" ] && bin/factory run "$evt" --retries 1
  mark 2 end; sleep "$GAP"
fi
if case_on 3; then
  mark 3 start
  python3 demo/show_feedback.py intake/feedback/samples/07b-fake-maintainer-note.json --highlight "Maintainer note" --hold 5
  mark 3 end; sleep "$GAP"
fi
n=4
for evt in factory/examples/evt-s8-from-now.json factory/examples/evt-timetable-stale.json \
           factory/examples/evt-s4-checkout.json factory/examples/evt-s9-porto-zone.json; do
  if case_on $n; then
    mark $n start
    bin/factory run "$evt" --retries 1
    mark $n end; sleep "$GAP"
  fi
  n=$((n + 1))
done

sleep 15
kill -TERM -- -"$REC" 2>/dev/null || kill -TERM "$REC"; wait "$REC" 2>/dev/null
kill -TERM -- -"$SERVE" 2>/dev/null || kill "$SERVE"
for f in runs/run-*/builder.cast runs/run-*/feedback.cast; do
  [ -f "$f" ] && cp "$f" "$OUT/casts/$(basename "$(dirname "$f")")-$(basename "$f")"
done
bin/factory costs > "$OUT/costs.txt" 2>&1
bin/factory rules > "$OUT/rules.txt" 2>&1
cp runs/events.jsonl "$OUT/events.jsonl"
echo "== done $(date +%T)"
