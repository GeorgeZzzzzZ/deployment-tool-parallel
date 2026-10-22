#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DEPLOY_CONSOLE_VENV="${DEPLOY_CONSOLE_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/deploy-console/venv-deepin}" \
  bash build-linux.sh

architecture="$(uname -m)"
package="dist/DeployConsoleParallel-deepin25-${architecture}.tar.gz"
cp "dist/DeployConsoleParallel-linux-${architecture}.tar.gz" "$package"

echo "Build complete: dist/DeployConsoleParallel"
echo "Package complete: $package"
