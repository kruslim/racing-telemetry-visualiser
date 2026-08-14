#!/bin/bash
# run_pitwall.sh - headless staged build of RTV v2 pitwall (bash / Git Bash / WSL)
set -e

[ -d prompts ] || { echo "No ./prompts folder. Run from the repo root."; exit 1; }
mkdir -p prompts/done

shopt -s nullglob
files=(prompts/[0-9]*.md)
if [ ${#files[@]} -eq 0 ]; then
  echo "All stages already completed."
  exit 0
fi

for f in "${files[@]}"; do
  name=$(basename "$f" .md)
  echo ""
  echo "=== [$name] starting ==="
  claude -p "$(cat "$f")" --dangerously-skip-permissions

  echo "=== [$name] verifying with pytest ==="
  if ! pytest -q; then
    echo "pytest FAILED after $name - stopping. Fix, then re-run (done stages are skipped)."
    exit 1
  fi

  git add -A
  git commit -m "pitwall: $name"

  mv "$f" prompts/done/
  git add -A
  git commit -m "pitwall: mark $name done" --allow-empty

  echo "=== [$name] committed ==="
done

echo ""
echo "All stages complete. See docs/PITWALL.md for the demo path."
