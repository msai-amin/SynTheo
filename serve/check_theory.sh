#!/usr/bin/env bash
# check-theory <theory-file> [parent-session]
#
# Baked into syntheo-isabelle (serve/isabelle.Dockerfile) as /usr/local/bin/check-theory.
# Builds the submitted theory against a prebuilt parent heap and emits ONE machine-
# readable verdict line that core/verify/isabelle_hol.py parses:
#
#   RESULT: verified      -- isabelle build succeeded: the kernel checked every proof
#   RESULT: refuted       -- a Nitpick counterexample / inconsistency was reported
#   RESULT: unverifiable  -- neither (open goal, timeout, missing lemma)
#
# The container is already the security boundary (no network, read-only, mem/pids
# caps applied by the caller); this script only orchestrates the build.
set -uo pipefail

THY="${1:?usage: check-theory <theory-file> [parent-session]}"
PARENT="${2:-GoedelGod}"
WORK=/work

# Read-only-rootfs bridge: the prebuilt parent heaps live in the SYSTEM heaps dir
# (baked, read-only). system_heaps=false reads only the USER heaps dir (a writable
# tmpfs here), so symlink the system heap files into it — reads resolve through the
# links, while the Submission session's own output writes as real files alongside.
SYS=$(isabelle getenv -b ISABELLE_HEAPS_SYSTEM)
USR=$(isabelle getenv -b ISABELLE_HEAPS)
if [ -n "$SYS" ] && [ -d "$SYS" ]; then
  for mldir in "$SYS"/*/; do
    ml=$(basename "$mldir")
    mkdir -p "$USR/$ml/log"
    for f in "$mldir"*; do [ -f "$f" ] && ln -sf "$f" "$USR/$ml/"; done
    for f in "$mldir"log/*; do [ -f "$f" ] && ln -sf "$f" "$USR/$ml/log/"; done
  done
fi

NAME=$(grep -m1 -oP '^\s*theory\s+\K[A-Za-z][A-Za-z0-9_'"'"']*' "$THY")
if [ -z "${NAME:-}" ]; then
  echo "no theory header found"
  echo "RESULT: unverifiable"
  exit 0
fi

cp "$THY" "$WORK/$NAME.thy"
cat > "$WORK/ROOT" <<EOF
session Submission = $PARENT +
  theories $NAME
EOF

OUT=$(isabelle build -d "$WORK" -o quick_and_dirty=false Submission 2>&1)
CODE=$?
echo "$OUT"

# Batch `isabelle build` suppresses Nitpick's diagnostic text, so we can't grep for
# "found a counterexample". Instead we use `nitpick [expect = genuine]`: that annotation
# makes the command RAISE (build fails) unless Nitpick found a genuine countermodel. So a
# theory that carries `expect = genuine` and still builds cleanly IS a confirmed
# refutation — a countermodel exists. This is the refutation convention for submissions.
if [ $CODE -eq 0 ] && grep -qiE 'nitpick[^A-Za-z].*expect[[:space:]]*=[[:space:]]*genuine' "$WORK/$NAME.thy"; then
  echo "RESULT: refuted"
elif [ $CODE -eq 0 ]; then
  echo "RESULT: verified"
else
  echo "RESULT: unverifiable"
fi
