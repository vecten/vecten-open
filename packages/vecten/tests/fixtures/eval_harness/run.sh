#!/bin/bash
# run '<shell command>': runs it in the project folder with the evaluation's environment, logged.
BASE=@@BASE@@
PYBIN=@@PYBIN@@
DEMO_DOC=@@DEMO_DOC@@
LOG="$BASE/log"
export PATH="$BASE/bin:$PYBIN:$PATH"
. @@ENV@@
cd @@PROJECT@@ || exit 1
n=$(printf %03d $(( $(ls "$LOG" | grep -c '\.cmd$') + 1 )))
printf '%s\n' "$*" > "$LOG/$n.cmd"
bash -c "$*" < /dev/null 2>&1 | tee "$LOG/$n.out"
rc=${PIPESTATUS[0]}
echo "$rc" > "$LOG/$n.rc"
# The case's API documentation is the person's to supply once `vecten init` made the project;
# the note goes to the harness's log so the agent's output stays its command's own.
if [ -n "$DEMO_DOC" ] && [ -f vecten.toml ] && [ ! -f docs/demo-api.md ]; then
  mkdir -p docs && cp "$DEMO_DOC" docs/demo-api.md &&
    echo "after $n: copied docs/demo-api.md into the project" >> "$LOG/harness.log"
fi
echo "[exit $rc]"
exit "$rc"
