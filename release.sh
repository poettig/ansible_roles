#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 [-n] <patch|minor|major>" >&2
  echo "  -n    skip push (local commit and tag only)" >&2
}

die() { echo "error: $*" >&2; exit 1; }

main() {
  local no_push=0
  while getopts ":n" opt; do
    case "$opt" in
      n) no_push=1 ;;
      \?) usage; exit 2 ;;
    esac
  done
  shift $((OPTIND - 1))

  if [[ $# -ne 1 ]]; then
    usage
    exit 2
  fi
  local kind=$1
  case "$kind" in
    patch|minor|major) ;;
    *)
      usage
      exit 2
      ;;
  esac

  # 1. Must be on main
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD)"
  [[ "$branch" == "main" ]] || die "must be on main (currently on $branch)"

  # 2. Working tree clean
  git diff --quiet || die "working tree not clean (unstaged changes)"
  git diff --cached --quiet || die "working tree not clean (staged changes)"
  [[ -z "$(git ls-files --others --exclude-standard)" ]] || die "working tree not clean (untracked files)"

  # 3. galaxy.yml has a version line
  [[ -f galaxy.yml ]] || die "galaxy.yml not found"
  local current
  current="$(grep -E '^version: [0-9]+\.[0-9]+\.[0-9]+$' galaxy.yml | head -1 | awk '{print $2}')"
  [[ -n "$current" ]] || die "no version found in galaxy.yml"

  # 4. Compute new version
  local major minor patch new
  IFS='.' read -r major minor patch <<< "$current"
  case "$kind" in
    major) new="$((major+1)).0.0" ;;
    minor) new="$major.$((minor+1)).0" ;;
    patch) new="$major.$minor.$((patch+1))" ;;
  esac

  # 5. Tag must not already exist
  if git rev-parse -q --verify "refs/tags/$new" >/dev/null; then
    die "tag $new already exists"
  fi

  echo "bumping $current -> $new"
  sed -i -E "s/^version: [0-9]+\.[0-9]+\.[0-9]+$/version: $new/" galaxy.yml

  echo "committing"
  git add galaxy.yml
  git commit -qm "release: $new"

  echo "tagging"
  git tag "$new"

  if [[ $no_push -eq 1 ]]; then
    echo "skipping push (-n)"
  else
    echo "pushing"
    if ! git push -q || ! git push -q origin "$new"; then
      die "push failed. Local commit and tag '$new' are ready. Retry: git push && git push origin $new"
    fi
  fi

  echo "released $new"
}

main "$@"
