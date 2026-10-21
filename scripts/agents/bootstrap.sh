#!/usr/bin/env bash
# Bring a fresh Linux VM (instabox or similar) up to the state the HAT agent workflow expects.
# Idempotent: re-running skips whatever is already in place.
#
#   bash scripts/agents/bootstrap.sh            # from the main clone
#
# Layout (override the root with HAT_WS):
#   $HAT_WS/algotrading      main clone (this repo), with venv/ at its root
#   $HAT_WS/wt/<branch>      one git worktree per PR
#   $HAT_WS/.tools           node, JDK, memwatch log
#
# Needs: git, gh (signed in as a repo writer), curl, docker, uv. Sign-ins are never scripted:
# run `gh auth status` and `claude` yourself first.
set -euo pipefail

HAT_WS=${HAT_WS:-/data/workspace}
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
TOOLS=$HAT_WS/.tools
NODE_VER=v26.12.0
JDK_URL=https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse
mkdir -p "$TOOLS" "$HAT_WS/wt"

step() { printf '\n== %s\n' "$*"; }

step "gh sign-in"
gh auth status >/dev/null 2>&1 || { echo "gh is not signed in: run 'gh auth login' first"; exit 1; }

step "Python 3.14 venv ($REPO/venv)"
if [ ! -x "$REPO/venv/bin/python" ]; then
  uv python install 3.14
  uv venv --python 3.14 --seed "$REPO/venv"
fi
"$REPO/venv/bin/python" "$REPO/scripts/install_backend_deps.py"
"$REPO/venv/bin/python" -m pip install -q ruff

step "Node $NODE_VER ($TOOLS)"
if [ ! -x "$TOOLS/node-$NODE_VER-linux-x64/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" | tar -xJ -C "$TOOLS"
fi

step "JDK 21 for TLC ($TOOLS/jdk)"
if [ ! -x "$TOOLS/jdk/bin/java" ]; then
  mkdir -p "$TOOLS/jdk"
  curl -fsSL "$JDK_URL" | tar -xz -C "$TOOLS/jdk" --strip-components=1
fi

step "Postgres container hatpg (localhost:5432, no password, like CI)"
if ! docker ps --format '{{.Names}}' | grep -qx hatpg; then
  docker start hatpg 2>/dev/null || docker run -d --name hatpg --restart unless-stopped \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=smarttrader_test -p 5432:5432 postgres:18.6
fi

step "4 GB swap (the 8 GB VM ran out of memory twice without it)"
if [ "$(tail -n +2 /proc/swaps | wc -l)" -eq 0 ]; then
  sw=$HAT_WS/../swapfile
  [ -f "$sw" ] || { sudo fallocate -l 4G "$sw"; sudo chmod 600 "$sw"; sudo mkswap "$sw" >/dev/null; }
  sudo swapon "$sw" || echo "swapon failed; continue without swap"
fi

step "herdr (terminal workspace for the builder agents)"
command -v herdr >/dev/null || curl -fsSL https://herdr.dev/install.sh | sh

cat <<EOF

Done. Next:
  export PATH=$TOOLS/node-$NODE_VER-linux-x64/bin:$TOOLS/jdk/bin:\$PATH
  herdr                                  # start or attach the session; run claude inside it
  claude --permission-mode auto          # then: /hat-orchestrator
EOF
