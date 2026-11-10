#!/bin/bash
# human '<message>': puts the message to the person and prints their reply.
# human --wait <n>: keeps waiting for the reply to message <n>.
DIR=@@INBOX@@
HUMAN=@@HUMAN@@
if [ "$1" = "--wait" ]; then n=$2; else
  n=$(printf %03d $(( $(ls "$DIR" | grep -c '\.ask$') + 1 )))
  printf '%s\n' "$*" > "$DIR/$n.tmp" && mv "$DIR/$n.tmp" "$DIR/$n.ask"
fi
# The Bash tool stops a call after ten minutes, so the wait ends before that.
for _ in $(seq 1 "${EVAL_HARNESS_HUMAN_POLLS:-270}"); do
  if [ -f "$DIR/$n.reply" ]; then cat "$DIR/$n.reply"; exit 0; fi
  sleep 2
done
echo "(no reply yet to message $n; run: $HUMAN --wait $n)"
exit 3
