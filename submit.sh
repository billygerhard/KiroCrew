#!/usr/bin/env bash
# File every draft in pending/ on kirodotdev/KiroCrew, then move it to submitted/.
set -euo pipefail

REPO=https://github.com/kirodotdev/KiroCrew
cd "$(dirname "$0")"

git pull --ff-only -q origin upstream-issues

shopt -s nullglob
drafts=(pending/*.md)
[ ${#drafts[@]} -eq 0 ] && exit 0

mkdir -p submitted
filed=0
for f in "${drafts[@]}"; do
  # Front matter is the block between the first two '---' lines.
  title=$(awk '/^---$/{n++; next} n==1 && /^title:/{sub(/^title:[ ]*/, ""); print; exit}' "$f")
  labels=$(awk '/^---$/{n++; next} n==1 && /^labels:/{sub(/^labels:[ ]*/, ""); sub(/[ ]*#.*/, ""); print; exit}' "$f")
  if [ -z "$title" ]; then
    echo "skip $f: no title" >&2
    continue
  fi

  body=$(mktemp -t kc-issue-body.XXXXXX)
  awk 'n>=2{print; next} /^---$/{n++}' "$f" > "$body"

  label_args=()
  IFS=',' read -ra parts <<< "$labels"
  for l in "${parts[@]}"; do
    l=$(echo "$l" | sed 's/^ *//; s/ *$//')
    [ -n "$l" ] && label_args+=(--label "$l")
  done

  if ! url=$(gh issue create --repo "$REPO" --title "$title" --body-file "$body" "${label_args[@]}" 2>/dev/null); then
    url=$(gh issue create --repo "$REPO" --title "$title" --body-file "$body")
  fi
  rm -f "$body"

  dest="submitted/$(basename "$f")"
  awk -v url="$url" 'NR==1 && /^---$/{print; print "issue: " url; next} {print}' "$f" > "$dest"
  git rm -q "$f"
  git add "$dest"
  echo "filed $f -> $url"
  filed=$((filed + 1))
done

if [ "$filed" -gt 0 ]; then
  git commit -qm "Submit $filed upstream issue(s)"
  git push -q origin HEAD:upstream-issues
fi
