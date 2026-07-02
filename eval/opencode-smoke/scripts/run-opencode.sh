#!/usr/bin/env bash
# Wrapper: run opencode in the workspace, copy new files to output_dir,
# extract metrics from JSON event stream.
#
# Usage: run-opencode.sh <prompt> <workspace> <output_dir> [model]
set -euo pipefail

PROMPT="$1"
WORKSPACE="$2"
OUTPUT_DIR="$3"
MODEL="${4:-google-vertex/claude-haiku-4-5@20251001}"

OPENCODE="${OPENCODE_BIN:-$(command -v opencode 2>/dev/null || echo "$HOME/.opencode/bin/opencode")}"

mkdir -p "$WORKSPACE" "$OUTPUT_DIR"
cd "$WORKSPACE"

# Snapshot pre-existing files with checksums to detect both new and modified files
PRE_CHECKSUMS=$(mktemp)
EVENT_LOG=$(mktemp)
POST_CHECKSUMS=$(mktemp)
trap 'rm -f "$PRE_CHECKSUMS" "$POST_CHECKSUMS" "$EVENT_LOG"' EXIT

find . -maxdepth 3 -type f \
  -not -path './.git/*' -not -path './.opencode/*' \
  -not -path './output/*' -not -path './.claude/*' \
  -exec md5sum {} + 2>/dev/null | sort > "$PRE_CHECKSUMS" || true

# Run opencode — capture JSON events to a temp file, stream to stdout for harness
set +e
"$OPENCODE" run "$PROMPT" \
  --format json \
  --dir "$WORKSPACE" \
  --model "$MODEL" \
  --auto \
  2>&1 | tee "$EVENT_LOG"
EXIT_CODE=${PIPESTATUS[0]}
set -e

# Snapshot post-run files with checksums
find . -maxdepth 3 -type f \
  -not -path './.git/*' -not -path './.opencode/*' \
  -not -path './output/*' -not -path './.claude/*' \
  -exec md5sum {} + 2>/dev/null | sort > "$POST_CHECKSUMS" || true

# Copy new and modified files to output_dir
comm -13 "$PRE_CHECKSUMS" "$POST_CHECKSUMS" | awk '{print $2}' | while IFS= read -r f; do
  target_dir="$OUTPUT_DIR/$(dirname "$f")"
  mkdir -p "$target_dir"
  cp "$f" "$OUTPUT_DIR/$f" 2>/dev/null || true
done

# Extract metrics from step_finish events in the JSON stream
_EVENT_LOG="$EVENT_LOG" _OUTPUT_DIR="$OUTPUT_DIR" _MODEL="$MODEL" \
python3 -c "
import json, os

total_input = 0
total_output = 0
cache_read = 0
cache_write = 0
total_cost = 0.0
num_turns = 0

for line in open(os.environ['_EVENT_LOG']):
    line = line.strip()
    if not line or not line.startswith('{'):
        continue
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        continue
    if evt.get('type') == 'step_finish':
        part = evt.get('part', {})
        tokens = part.get('tokens', {})
        total_input += tokens.get('input', 0)
        total_output += tokens.get('output', 0)
        cache_read += tokens.get('cache', {}).get('read', 0)
        cache_write += tokens.get('cache', {}).get('write', 0)
        total_cost += part.get('cost', 0.0)
        num_turns += 1

metrics = {
    'token_usage': {
        'input': total_input + cache_read,
        'output': total_output,
        'cache_read': cache_read,
        'cache_write': cache_write,
    },
    'cost_usd': round(total_cost, 6) if num_turns > 0 else None,
    'num_turns': num_turns if num_turns > 0 else None,
    'model': os.environ['_MODEL'],
}
with open(os.path.join(os.environ['_OUTPUT_DIR'], 'metrics.json'), 'w') as f:
    json.dump(metrics, f, indent=2)
" 2>/dev/null || true

exit $EXIT_CODE
