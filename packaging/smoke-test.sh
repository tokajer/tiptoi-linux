#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
readonly REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
# WHY: a relative path here makes podman reject the bind mount (it requires an absolute host
# path), which podman then reports as a mount failure that looks like an unrelated artifact problem.
readonly APPIMAGE_PATH="$(realpath -- "${1:-${REPO_ROOT}/tiptoi-x86_64.AppImage}")"
readonly DISTROS=(ubuntu:22.04 debian:12)

if ! command -v podman >/dev/null 2>&1; then
    echo "error: podman is required to run the smoke test" >&2
    exit 1
fi

if [[ ! -s "$APPIMAGE_PATH" ]]; then
    echo "error: AppImage not found or empty: $APPIMAGE_PATH" >&2
    exit 1
fi

# WHY: this list is the documented host runtime requirement set for this AppImage - the
# AppImage-excludelist libraries that must stay host-provided (libGL/libEGL/libX11/libxcb/libglib/
# libfontconfig/libfreetype/libdbus) PLUS the Qt6 xcb-util/xkbcommon family. That family is not
# bundled because the only copies available on this build host are glibc-2.43 Fedora binaries, and
# bundling those would raise the AppImage's floor from 2.34 to 2.43. AppRun's preflight names exactly
# these packages to the user when they are missing. A bare container otherwise supplies almost none
# of the libraries the bundle needs at runtime, which used to fail both distros on libGL.so.1 alone
# and report FAIL on a bundle that was actually good; installing this full set - and nothing else -
# fixes that false negative without masking a genuine missing-library regression.
readonly CONTAINER_SCRIPT='
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    libgl1 libegl1 libx11-6 libx11-xcb1 libxcb1 libglib2.0-0 libfontconfig1 libfreetype6 \
    libdbus-1-3 libxcb-glx0 libxcb-randr0 libxcb-render0 libxcb-shape0 libxcb-shm0 \
    libxcb-sync1 libxcb-xfixes0 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-util1 \
    libxcb-xkb1 ca-certificates >/dev/null

mkdir -p /work
cd /work
cp /appimage/tiptoi.AppImage ./tiptoi.AppImage
chmod +x ./tiptoi.AppImage
./tiptoi.AppImage --appimage-extract >/dev/null

PYBIN="./squashfs-root/opt/python3.12/bin/python3.12"
CERTS="./squashfs-root/opt/_internal/certs.pem"

QT_QPA_PLATFORM=offscreen SSL_CERT_FILE="$CERTS" "$PYBIN" -c "
from PySide6 import __version__ as qt_version
from PySide6.QtWidgets import QApplication
from tiptoi_linux.gui import MainWindow

app = QApplication([])
MainWindow()
print(\"QT_VERSION_OK:\" + qt_version)
"

SSL_CERT_FILE="$CERTS" "$PYBIN" -c "
from tiptoi_linux.catalog import load_catalog

c = load_catalog(force=True).catalog
assert len(c.products) > 250, f\"expected >250 products, got {len(c.products)}\"
print(\"PRODUCT_COUNT_OK:\" + str(len(c.products)))
"

# WHY: this is the assertion that would have caught the APPDIR-depth and preflight-ordering bugs -
# the two checks above exercise the bundled interpreter directly and never touch AppRun itself.
# AppRun execs the GUI event loop and never returns on success, so a short timeout plus a check
# that its stderr carries neither failure signature is the pass condition, not a clean exit.
apprun_stderr=/tmp/apprun.stderr
QT_QPA_PLATFORM=offscreen SSL_CERT_FILE="$CERTS" timeout 5 ./squashfs-root/AppRun >/tmp/apprun.stdout 2>"$apprun_stderr" || true
if grep -qF "No such file or directory" "$apprun_stderr" || grep -qF "ImportError" "$apprun_stderr"; then
    echo "AppRun stderr:" >&2
    cat "$apprun_stderr" >&2
    echo "APPRUN_FAIL"
    exit 1
fi
echo "APPRUN_OK"
'

overall_status=0

for distro in "${DISTROS[@]}"; do
    echo "==> smoke test: ${distro}"
    if output=$(podman run --rm \
        -v "${APPIMAGE_PATH}:/appimage/tiptoi.AppImage:ro" \
        "${distro}" \
        bash -c "$CONTAINER_SCRIPT" 2>&1); then
        qt_line=$(printf '%s\n' "$output" | grep -o 'QT_VERSION_OK:[^[:space:]]*' || true)
        count_line=$(printf '%s\n' "$output" | grep -o 'PRODUCT_COUNT_OK:[0-9]*' || true)
        apprun_line=$(printf '%s\n' "$output" | grep -o 'APPRUN_OK' || true)
        if [[ -n "$qt_line" && -n "$count_line" && -n "$apprun_line" ]]; then
            echo "PASS: ${distro} (Qt ${qt_line#QT_VERSION_OK:}, ${count_line#PRODUCT_COUNT_OK:} products, AppRun OK)"
        else
            echo "FAIL: ${distro} (expected markers not found in output)"
            printf '%s\n' "$output"
            overall_status=1
        fi
    else
        echo "FAIL: ${distro} (container run failed)"
        printf '%s\n' "$output"
        overall_status=1
    fi
done

exit "$overall_status"
