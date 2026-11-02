#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bin_dir=${CODEX_HOME:-$HOME/.codex}/bin
skill_dir=${CODEX_HOME:-$HOME/.codex}/skills
with_skill=1
while [[ $# -gt 0 ]]; do
  case $1 in
    --bin-dir|--skill-dir)
      [[ $# -ge 2 && -n $2 ]] || { echo "$1 requires a directory" >&2; exit 2; }
      if [[ $1 == --bin-dir ]]; then bin_dir=$2; else skill_dir=$2; fi
      shift 2 ;;
    --no-skill) with_skill=0; shift ;;
    -h|--help)
      echo 'Usage: ./install.sh [--bin-dir DIR] [--skill-dir DIR] [--no-skill]'
      echo 'Defaults: $CODEX_HOME/bin and $CODEX_HOME/skills (CODEX_HOME defaults to ~/.codex).'
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
install -d -- "$bin_dir"
install -m 0755 -- "$root/bin/codex-bg" "$bin_dir/codex-bg"
printf 'Installed script: %s/codex-bg\n' "$bin_dir"
if [[ $with_skill == 1 ]]; then
  install -d -- "$skill_dir/codex-bg/agents"
  install -m 0644 -- "$root/skills/codex-bg/SKILL.md" "$skill_dir/codex-bg/SKILL.md"
  install -m 0644 -- "$root/skills/codex-bg/agents/openai.yaml" "$skill_dir/codex-bg/agents/openai.yaml"
  printf 'Installed skill: %s/codex-bg\n' "$skill_dir"
fi
printf 'Add %s to PATH if needed. No shell profile was changed.\n' "$bin_dir"
