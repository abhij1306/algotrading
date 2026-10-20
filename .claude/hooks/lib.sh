#!/usr/bin/env bash
# Shared helpers for project hooks. Hook input arrives as JSON on stdin.

# Print tool_input.file_path from the hook JSON on stdin, with forward slashes.
hook_file_path() {
  node -e '
    let s = "";
    process.stdin.on("data", (d) => (s += d)).on("end", () => {
      try {
        const p = JSON.parse(s)?.tool_input?.file_path ?? "";
        process.stdout.write(p.replaceAll("\\", "/"));
      } catch { /* not JSON: print nothing */ }
    });
  '
}

repo_root() {
  git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel
}

# Path of $1 relative to $2, or empty if $1 is not under $2. Case-insensitive for Windows drives.
rel_to() {
  local file="$1" base="$2"
  local lf="${file,,}" lb="${base,,}"
  case "$lf" in
    "$lb"/*) printf '%s' "${file:${#base}+1}" ;;
    *) printf '' ;;
  esac
}
