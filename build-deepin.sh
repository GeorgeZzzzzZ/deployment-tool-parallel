#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

venv_dir="${XDG_CACHE_HOME:-$HOME/.cache}/deploy-console/venv-deepin"
python3 -m venv "$venv_dir"
source "$venv_dir/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean DeployConsole.spec

dist/DeployConsoleParallel --self-test-credentials
dist/DeployConsoleParallel --self-test-selenium

architecture="$(uname -m)"
package="dist/DeployConsoleParallel-deepin25-${architecture}.tar.gz"
tar -C dist -czf "$package" DeployConsoleParallel

echo "Build complete: dist/DeployConsoleParallel"
echo "Package complete: $package"
