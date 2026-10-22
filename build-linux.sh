#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Build failed: run build-linux.sh on Linux." >&2
  exit 1
fi

cd "$(dirname "$0")"

venv_dir="${DEPLOY_CONSOLE_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/deploy-console/venv-linux}"
python3 -m venv "$venv_dir"
source "$venv_dir/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean DeployConsole.spec

dist/DeployConsoleParallel --self-test-credentials
dist/DeployConsoleParallel --self-test-selenium

architecture="$(uname -m)"
package="dist/DeployConsoleParallel-linux-${architecture}.tar.gz"
tar -C dist -czf "$package" DeployConsoleParallel

echo "Build complete: dist/DeployConsoleParallel"
echo "Package complete: $package"
