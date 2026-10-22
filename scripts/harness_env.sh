# Sourced, not run. Loads harness.toml into HARNESS_* variables for a bash control.
#
#   . "$(dirname "$0")/harness_env.sh"
#   harness_load "$ROOT" || exit 1
#
# The reader is located beside this file, not under $ROOT, so a control can read
# another tree's config (land.sh reads the candidate's) with its own reader.
HARNESS_SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

harness_load(){
  if python3 -c 'import tomllib' 2>/dev/null; then
    HARNESS_PY=(python3)
  elif command -v uv >/dev/null 2>&1; then
    HARNESS_PY=(uv run --quiet --no-project python3)
  else
    echo "✗ harness: reading harness.toml needs Python 3.11+ or uv" >&2
    return 1
  fi
  local out
  out=$("${HARNESS_PY[@]}" "$HARNESS_SCRIPTS/harness_config.py" --root "$1" --shell) || return 1
  eval "$out"
}
