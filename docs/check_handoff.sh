#!/usr/bin/env bash
# Handoff-system hygiene check. Fails loud if a layer is drifting back to the old "append-only pile".
# Run from the repo root:  bash docs/check_handoff.sh
# See docs/CONVENTIONS.md for the rules these caps enforce.
set -u
fail=0

repo="$(cd "$(dirname "$0")/.." && pwd)"
mem_index="$HOME/.claude/projects/-home-yusheng-cadence-work-Test-workarea-LDO-modeling/memory/MEMORY.md"
mem_dir="$(dirname "$mem_index")"

STATUS_MAX_LINES=150
MEMINDEX_MAX_KB=24
MEMFILE_MAX_KB=8

note() { printf '  %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fail=1; }
ok()   { printf 'ok    %s\n' "$1"; }

# 1. STATUS.md line cap
if [ -f "$repo/STATUS.md" ]; then
  n=$(wc -l < "$repo/STATUS.md")
  if [ "$n" -gt "$STATUS_MAX_LINES" ]; then
    bad "STATUS.md is $n lines (cap $STATUS_MAX_LINES) — move detail to threads/ or reference/"
  else ok "STATUS.md $n lines"; fi
else bad "STATUS.md missing"; fi

# 2. MEMORY.md index size cap (it is truncated on load if over this → every session starts blind)
if [ -f "$mem_index" ]; then
  kb=$(( ( $(wc -c < "$mem_index") + 1023 ) / 1024 ))
  if [ "$kb" -gt "$MEMINDEX_MAX_KB" ]; then
    bad "MEMORY.md index is ${kb}KB (cap ${MEMINDEX_MAX_KB}KB) — shorten index entries to one line each"
  else ok "MEMORY.md index ${kb}KB"; fi
fi

# 3. no oversized individual memory file (bigger = a handoff, not a fact)
if [ -d "$mem_dir" ]; then
  while IFS= read -r f; do
    [ "$(basename "$f")" = "MEMORY.md" ] && continue
    kb=$(( ( $(wc -c < "$f") + 1023 ) / 1024 ))
    if [ "$kb" -gt "$MEMFILE_MAX_KB" ]; then
      bad "memory/$(basename "$f") is ${kb}KB (cap ${MEMFILE_MAX_KB}KB) — split it or move status to STATUS.md"
    fi
  done < <(find "$mem_dir" -maxdepth 1 -name '*.md')
fi

# 4. no live handoff journal creeping back into the repo root (history belongs in docs/archive/)
stray=$(find "$repo" -maxdepth 1 -name 'HANDOFF*.md' 2>/dev/null)
if [ -n "$stray" ]; then
  bad "HANDOFF*.md in repo root — move superseded handoffs to docs/archive/:"
  printf '%s\n' "$stray" | while read -r s; do note "$(basename "$s")"; done
else ok "no stray HANDOFF*.md in root"; fi

# 5. reference/ docs must not carry status words (they are durable facts only)
for r in DATA TOOL_FACTS METHODOLOGY; do
  p="$repo/docs/reference/$r.md"
  [ -f "$p" ] || continue
  # NB: SUPERSEDED/REJECTED are legitimate durable VERDICT labels in METHODOLOGY — not flagged.
  # Only flag genuine current-task status tells.
  if grep -qiE 'next session|TODO:|FIXME:|awaiting bash apply|box-validate pending' "$p"; then
    bad "docs/reference/$r.md contains status/changelog words — keep it durable-facts-only"
  fi
done

echo
if [ "$fail" -eq 0 ]; then echo "PASS — handoff system within caps"; else echo "DRIFT detected — see FAIL lines above"; fi
exit $fail
