#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'macOS 安装包只能在 macOS 上构建。\n'
  exit 1
fi

BUILD_PYTHON="${FITMIX_BUILD_PYTHON:-python3}"
if ! command -v "$BUILD_PYTHON" >/dev/null 2>&1; then
  printf '未找到 Python 3。请先安装 Python 3.11 或更高版本。\n'
  exit 1
fi
BUILD_PYTHON="$(command -v "$BUILD_PYTHON")"
if ! "$BUILD_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf '需要 Python 3.11 或更高版本。\n'
  exit 1
fi

PYTHON_TAG="$($BUILD_PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
BUILD_VENV=".build-venv-$PYTHON_TAG"
if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
  "$BUILD_PYTHON" -m venv "$BUILD_VENV"
fi

"$BUILD_VENV/bin/python" -m pip install --disable-pip-version-check --quiet \
  -r requirements.txt pyinstaller==6.22.2 pywebview==6.2.1

VERSION="${FITMIX_VERSION:-0.1.1}"
ARCH="$(uname -m)"
APP_PATH="$PWD/dist/FitMix.app"
DMG_NAME="FitMix-macOS-${ARCH}.dmg"

"$BUILD_VENV/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name FitMix \
  --icon "$PWD/scripts/macos/FitMix.icns" \
  --osx-bundle-identifier com.betterjally.fitmix \
  --add-data "$PWD/fitmix/templates:fitmix/templates" \
  --add-data "$PWD/fitmix/static:fitmix/static" \
  --hidden-import webview.platforms.cocoa \
  --distpath dist \
  --workpath build/pyinstaller \
  --specpath build \
  fitmix/desktop.py

PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

DMG_STAGE="$(mktemp -d /tmp/fitmix-dmg.XXXXXX)"
trap 'rm -rf "$DMG_STAGE"' EXIT
ditto "$APP_PATH" "$DMG_STAGE/FitMix.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -quiet -volname FitMix -srcfolder "$DMG_STAGE" -ov -format UDZO "dist/$DMG_NAME"
(cd dist && shasum -a 256 "$DMG_NAME" > "$DMG_NAME.sha256")

printf '完成：%s\n' "$APP_PATH"
printf '完成：%s/dist/%s\n' "$PWD" "$DMG_NAME"
