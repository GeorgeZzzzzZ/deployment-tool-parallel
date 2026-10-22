#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Build failed: run build-macos.sh on macOS." >&2
  exit 1
fi

cd "$(dirname "$0")"

python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Build failed: Python 3.9 or newer is required (current: ${python_version})." >&2
  exit 1
}
echo "Building macOS package with Python ${python_version}"

venv_dir="${DEPLOY_CONSOLE_VENV:-$HOME/Library/Caches/deploy-console/venv-macos}"
python3 -m venv "$venv_dir"
source "$venv_dir/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

icon_tmp="$(mktemp -d)"
iconset="$icon_tmp/deploy-console-parallel.iconset"
mkdir -p "$iconset"
cleanup_icon() {
  rm -f assets/deploy-console-parallel.icns
  rm -rf "$icon_tmp"
}
trap cleanup_icon EXIT

for size in 16 32 128 256 512; do
  sips -z "$size" "$size" assets/deploy-console-parallel.png \
    --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double_size=$((size * 2))
  sips -z "$double_size" "$double_size" assets/deploy-console-parallel.png \
    --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o assets/deploy-console-parallel.icns

python -m PyInstaller --noconfirm --clean DeployConsole.spec

# Fail the build before creating a DMG if the packaged app cannot save and
# decrypt Jenkins/Ops/Rancher credentials with its private local key.
dist/DeployConsoleParallel.app/Contents/MacOS/DeployConsoleParallel --self-test-credentials
dist/DeployConsoleParallel.app/Contents/MacOS/DeployConsoleParallel --self-test-selenium

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --deep --options runtime \
    --sign "$CODESIGN_IDENTITY" dist/DeployConsoleParallel.app
fi

hdiutil create -volname "Deploy Console Parallel" \
  -srcfolder dist/DeployConsoleParallel.app \
  -ov -format UDZO dist/DeployConsoleParallel-macos.dmg

echo "Build complete: dist/DeployConsoleParallel.app"
echo "Package complete: dist/DeployConsoleParallel-macos.dmg"
