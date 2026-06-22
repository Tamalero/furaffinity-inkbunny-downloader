#!/usr/bin/env bash
# Build a Type-2 AppImage for FA & Inkbunny Downloader.
#
# Usage:
#   ./build_appimage.sh              # reads VERSION from common.py
#   VERSION=1.2.0 ./build_appimage.sh
#
# Output: FurAffinityInkbunnyDownloader-<version>-x86_64.AppImage  (+ .zsync)
#
# Requirements on the build machine:
#   - Python 3.10+ with packages from requirements.txt installed
#   - pyinstaller + pillow (installed automatically into the active venv)
#   - appimagetool  (downloaded automatically to .build-tools/)
#   - zsync / zsyncmake (installed via pacman or apt if missing)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SCRIPT_DIR}/venv"
ARCH="x86_64"

# ── Activate venv ───────────────────────────────────────────────────────────────
if [ -f "${VENV}/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${VENV}/bin/activate"
    PYTHON="python3"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "Error: no venv at ${VENV} and python3 not on PATH." >&2
    exit 1
fi

# ── Resolve version ─────────────────────────────────────────────────────────────
VERSION="${VERSION:-$(${PYTHON} -c "import sys; sys.path.insert(0,'${SCRIPT_DIR}'); import common; print(common.VERSION)")}"
APPIMAGE_OUT="${SCRIPT_DIR}/FurAffinityInkbunnyDownloader-${VERSION}-${ARCH}.AppImage"
UPDATE_INFO="gh-releases-zsync|Tamalero|furaffinity-inkbunny-downloader|latest|FurAffinityInkbunnyDownloader-*-${ARCH}.AppImage.zsync"
APPDIR="${SCRIPT_DIR}/AppDir"

echo "==> FA & Inkbunny Downloader  v${VERSION}  (${ARCH})"

# ── Download appimagetool if missing ────────────────────────────────────────────
TOOLS="${SCRIPT_DIR}/.build-tools"
mkdir -p "${TOOLS}"
APPIMAGETOOL="${TOOLS}/appimagetool-${ARCH}.AppImage"

if [ ! -f "${APPIMAGETOOL}" ]; then
    echo "==> Downloading appimagetool…"
    curl -Lo "${APPIMAGETOOL}" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    chmod +x "${APPIMAGETOOL}"
fi

# ── Ensure zsyncmake ────────────────────────────────────────────────────────────
if ! command -v zsyncmake &>/dev/null; then
    echo "==> Installing zsync…"
    if command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm --needed zsync
    elif command -v apt-get &>/dev/null; then
        sudo apt-get install -y zsync
    else
        echo "Warning: zsync not found and could not be installed. .zsync will be skipped."
    fi
fi

# ── Install build-only Python deps ──────────────────────────────────────────────
echo "==> Installing build deps (pyinstaller, pillow)…"
${PYTHON} -m pip install --quiet pyinstaller pillow

# ── Generate icon ───────────────────────────────────────────────────────────────
if [ ! -f "${SCRIPT_DIR}/faib-downloader.png" ]; then
    echo "==> Generating icon…"
    ${PYTHON} "${SCRIPT_DIR}/create_icon.py"
fi

# ── PyInstaller ─────────────────────────────────────────────────────────────────
echo "==> Running PyInstaller…"
cd "${SCRIPT_DIR}"
${PYTHON} -m PyInstaller --clean --noconfirm faib-downloader.spec

# ── Assemble AppDir ─────────────────────────────────────────────────────────────
echo "==> Assembling AppDir…"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}"

cp -r "${SCRIPT_DIR}/dist/faib-downloader/." "${APPDIR}/"
cp "${SCRIPT_DIR}/AppRun"                    "${APPDIR}/AppRun"
cp "${SCRIPT_DIR}/faib-downloader.desktop"   "${APPDIR}/faib-downloader.desktop"
cp "${SCRIPT_DIR}/faib-downloader.png"       "${APPDIR}/faib-downloader.png"
ln -sf faib-downloader.png                   "${APPDIR}/.DirIcon"
chmod +x "${APPDIR}/AppRun"

# ── Build AppImage ──────────────────────────────────────────────────────────────
echo "==> Building AppImage…"
ARCH="${ARCH}" "${APPIMAGETOOL}" \
    --updateinformation "${UPDATE_INFO}" \
    "${APPDIR}" \
    "${APPIMAGE_OUT}"

# ── Generate .zsync for delta updates ───────────────────────────────────────────
if command -v zsyncmake &>/dev/null; then
    echo "==> Generating .zsync…"
    zsyncmake \
        -C "${APPIMAGE_OUT}" \
        -u "$(basename "${APPIMAGE_OUT}")" \
        -o "${APPIMAGE_OUT}.zsync"
fi

echo ""
echo "── Build complete ──────────────────────────────────────────────────────────"
echo "    ${APPIMAGE_OUT}"
[ -f "${APPIMAGE_OUT}.zsync" ] && echo "    ${APPIMAGE_OUT}.zsync"
echo ""
echo "To publish: git tag v${VERSION} && git push origin v${VERSION}"
echo "(GitHub Actions will build and attach the release assets automatically.)"
