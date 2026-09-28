#!/usr/bin/env bash
set -euo pipefail

readonly BASE_APPIMAGE_URL="https://github.com/niess/python-appimage/releases/download/python3.12/python3.12.14-cp312-cp312-manylinux_2_28_x86_64.AppImage"
readonly BASE_APPIMAGE_SHA256="cefdd1b6e08dfb6c977d4233a4177ed3ee55a526991a122c8d92263f5901544f"
readonly APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
readonly APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
readonly PYSIDE6_VERSION="6.11.2"

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
readonly REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
readonly BUILD_DIR="${SCRIPT_DIR}/build"
readonly CACHE_DIR="${BUILD_DIR}/cache"
readonly WORK_DIR="${BUILD_DIR}/work"
readonly OUTPUT_APPIMAGE="${REPO_ROOT}/tiptoi-x86_64.AppImage"

readonly UNUSED_QT_FAMILIES=(
    Qml Quick Designer Test Sql Multimedia Pdf Charts DataVisualization 3D
    WebSockets WebChannel WebEngine Bluetooth Nfc SerialPort Positioning
    RemoteObjects Scxml Sensors SpatialAudio TextToSpeech Help UiTools Concurrent
)
readonly KEEP_PLUGIN_DIRS=(platforms platformthemes imageformats iconengines xcbglintegrations)
readonly BUILD_TIME_ONLY_PACKAGES=(pip build pyproject_hooks packaging setuptools)

log() {
    echo "==> $*"
}

# WHY: a single guarded helper for every deletion in this script - a stray rm -rf here could
# destroy the project, so every call must prove its target sits inside our own build directory.
# Both sides are resolved with realpath -m first: a plain string-prefix case match is defeated by
# a target containing "..", e.g. "$BUILD_DIR/../../etc" string-matches "$BUILD_DIR/*" but resolves
# outside it.
safe_rm_rf() {
    local target="$1"
    if [[ -z "$target" ]]; then
        echo "error: refusing to rm -rf an empty path" >&2
        exit 1
    fi
    local resolved_target
    resolved_target="$(realpath -m -- "$target")"
    local resolved_build
    resolved_build="$(realpath -m -- "$BUILD_DIR")"
    case "$resolved_target" in
        "${resolved_build}"/*) ;;
        *)
            echo "error: refusing to rm -rf path outside BUILD_DIR: $target" >&2
            exit 1
            ;;
    esac
    rm -rf -- "$target"
}

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "error: required command not found: $cmd" >&2
        exit 1
    fi
}

sha256_of() {
    local file="$1"
    local digest
    digest="$(sha256sum -- "$file" | cut -d' ' -f1)"
    echo "$digest"
}

download_verified() {
    local url="$1" dest="$2" expected_sha256="$3"
    if [[ -s "$dest" ]]; then
        # WHY: a cache hit must be re-verified every time, not trusted forever - otherwise a
        # corrupted or tampered cache entry from a previous run silently poisons every later build.
        local cached_sha256
        cached_sha256="$(sha256_of "$dest")"
        if [[ "$cached_sha256" != "$expected_sha256" ]]; then
            echo "error: cached file failed checksum verification: $dest" >&2
            echo "  expected: $expected_sha256" >&2
            echo "  actual:   $cached_sha256" >&2
            exit 1
        fi
        log "cache hit (checksum verified): $(basename -- "$dest")"
        return 0
    fi
    log "downloading $(basename -- "$dest")"
    local tmp="${dest}.part"
    if ! curl --fail --location --show-error --silent --output "$tmp" "$url"; then
        echo "error: download failed for $url" >&2
        rm -f -- "$tmp"
        exit 1
    fi
    if [[ ! -s "$tmp" ]]; then
        echo "error: downloaded file is empty: $url" >&2
        rm -f -- "$tmp"
        exit 1
    fi
    local actual_sha256
    actual_sha256="$(sha256_of "$tmp")"
    if [[ "$actual_sha256" != "$expected_sha256" ]]; then
        echo "error: checksum verification failed for $url" >&2
        echo "  expected: $expected_sha256" >&2
        echo "  actual:   $actual_sha256" >&2
        rm -f -- "$tmp"
        exit 1
    fi
    mv -- "$tmp" "$dest"
}

convert_icon() {
    local svg="$1" png="$2"
    if command -v magick >/dev/null 2>&1; then
        magick -background none -density 384 "$svg" -resize 256x256 "$png"
    elif command -v convert >/dev/null 2>&1; then
        convert -background none -density 384 "$svg" -resize 256x256 "$png"
    elif command -v inkscape >/dev/null 2>&1; then
        inkscape "$svg" --export-type=png --export-filename="$png" -w 256 -h 256
    else
        echo "error: none of magick, convert, or inkscape found; cannot rasterize the icon" >&2
        exit 1
    fi
    if [[ ! -s "$png" ]]; then
        echo "error: icon conversion produced an empty or missing file: $png" >&2
        exit 1
    fi
}

trim_appdir() {
    local appdir="$1"
    local site="$2"
    local pyside_dir="${site}/PySide6"
    local qt_dir="${pyside_dir}/Qt"

    log "trimming unused Qt modules"

    if [[ -d "${qt_dir}/qml" ]]; then
        safe_rm_rf "${qt_dir}/qml"
    fi
    if [[ -d "${qt_dir}/translations" ]]; then
        # WHY: keep Qt's German strings (dialog buttons, context menus) for the German UI; every
        # other catalogue is dead weight since the app itself only ships English and German
        while IFS= read -r -d '' f; do
            safe_rm_rf "$f"
        done < <(find "${qt_dir}/translations" -mindepth 1 -maxdepth 1 ! -name 'qtbase_de.qm' -print0)
    fi

    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "$site" -name "*.pyi" -print0 2>/dev/null)

    # SHORTCUT: this is a hardcoded family-name allowlist, not a real dependency scan of the
    # bundled .so files; if a future PySide6 upgrade renames a module or adds a new unused Qt
    # family, it will silently stay bundled instead of being trimmed. Revisit with an ldd-based
    # reachability scan if bundle size ever needs to shrink further than this gets it.
    for family in "${UNUSED_QT_FAMILIES[@]}"; do
        if [[ -d "${qt_dir}/lib" ]]; then
            while IFS= read -r -d '' f; do
                safe_rm_rf "$f"
            done < <(find "${qt_dir}/lib" -maxdepth 1 -name "*Qt6${family}*" -print0 2>/dev/null)
        fi
        # WHY: -iname and a trailing * after .abi3.so - versioned module files like
        # libpyside6qml.abi3.so.6.11 are lowercase and don't end at ".abi3.so", so a case-sensitive,
        # anchored pattern silently lets them escape the trim.
        while IFS= read -r -d '' f; do
            safe_rm_rf "$f"
        done < <(find "$pyside_dir" -maxdepth 1 -iname "*${family}*.abi3.so*" -print0 2>/dev/null)
    done

    if [[ -d "${qt_dir}/plugins" ]]; then
        for d in "${qt_dir}/plugins"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name="$(basename -- "${d%/}")"
            local keep=false
            local k
            for k in "${KEEP_PLUGIN_DIRS[@]}"; do
                if [[ "$name" == "$k" ]]; then
                    keep=true
                    break
                fi
            done
            if [[ "$keep" == false ]]; then
                safe_rm_rf "${d%/}"
            fi
        done
    fi

    # WHY: enforces the recorded decision to bundle the xcb platform plugin only (PLANS.md) -
    # otherwise Qt on a Wayland session auto-picks the bundled libqwayland.so, whose
    # shell-integration plugins are removed by the KEEP_PLUGIN_DIRS filter above, and it aborts.
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${qt_dir}/plugins/platforms" -maxdepth 1 -name "libqwayland*.so" -print0 2>/dev/null)
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${qt_dir}/lib" -maxdepth 1 \( -name "libQt6Wayland*" -o -name "libQt6WlShellIntegration*" \) -print0 2>/dev/null)

    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${qt_dir}/lib" -maxdepth 1 \( -name "libQt6Labs*" -o -name "libQt6Lottie*" \
        -o -name "libQt6EglFSDeviceIntegration*" -o -name "libQt6EglFsKmsSupport*" \) -print0 2>/dev/null)

    if [[ -e "${qt_dir}/plugins/imageformats/libqpdf.so" ]]; then
        # WHY: its libQt6Pdf.so.6 is trimmed above (Pdf is in UNUSED_QT_FAMILIES), so this plugin
        # fails to load on every QImageReader construction if left behind.
        safe_rm_rf "${qt_dir}/plugins/imageformats/libqpdf.so"
    fi

    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${qt_dir}/lib" -maxdepth 1 -name "libQt6OpenGL*" -print0 2>/dev/null)
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "$pyside_dir" -maxdepth 1 -name "QtOpenGL*.abi3.so*" -print0 2>/dev/null)
    # WHY: these two platform plugins link libQt6OpenGL / libQt6EglFSDeviceIntegration, both trimmed
    # above; the app only uses xcb (offscreen/minimal for tests). A build host with a system Qt6
    # (e.g. Fedora) hides this - ldd resolves the missing libs from /usr/lib64 - but CI has none.
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${qt_dir}/plugins/platforms" -maxdepth 1 \( -name "libqeglfs.so" -o -name "libqminimalegl.so" \) -print0 2>/dev/null)

    local dev_exe
    for dev_exe in qmlls qmlformat qmllint assistant linguist lupdate lrelease designer svgtoqml; do
        if [[ -e "${pyside_dir}/${dev_exe}" ]]; then
            safe_rm_rf "${pyside_dir}/${dev_exe}"
        fi
    done
    local dev_dir
    for dev_dir in "${qt_dir}/metatypes" "${qt_dir}/libexec" "${pyside_dir}/include" \
        "${pyside_dir}/typesystems" "${pyside_dir}/glue" "${pyside_dir}/scripts"; do
        if [[ -d "$dev_dir" ]]; then
            safe_rm_rf "$dev_dir"
        fi
    done

    log "trimming build-time-only packages"
    # WHY: never touch site-packages/certifi here - opt/_internal/certs.pem symlinks into it and
    # every HTTPS download (the catalog fetch) depends on that symlink resolving.
    local pkg
    for pkg in "${BUILD_TIME_ONLY_PACKAGES[@]}"; do
        if [[ -d "${site}/${pkg}" ]]; then
            safe_rm_rf "${site}/${pkg}"
        fi
        while IFS= read -r -d '' f; do
            safe_rm_rf "$f"
        done < <(find "$site" -maxdepth 1 -iname "${pkg}-*.dist-info" -print0 2>/dev/null)
    done
    if [[ -d "${site}/bin" ]]; then
        # WHY: pip install --target bakes the BUILD MACHINE's absolute interpreter path into these
        # console-script shebangs, which exists on nobody else's machine, and bin/ is not on PATH
        # inside the AppImage anyway - pure leakage.
        safe_rm_rf "${site}/bin"
    fi

    log "trimming tcl/tk (unusable by a PySide6 app)"
    if [[ -d "${appdir}/usr/share/tcltk" ]]; then
        safe_rm_rf "${appdir}/usr/share/tcltk"
    fi
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${appdir}/usr/lib" -maxdepth 1 \( -name "libtk8.6.so*" -o -name "libtcl8.6.so*" \) -print0 2>/dev/null)
    if [[ -d "${appdir}/opt/python3.12/lib/python3.12/tkinter" ]]; then
        safe_rm_rf "${appdir}/opt/python3.12/lib/python3.12/tkinter"
    fi
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "${appdir}/opt/python3.12/lib/python3.12/lib-dynload" -maxdepth 1 -name "_tkinter*.so" -print0 2>/dev/null)

    if [[ -d "${appdir}/usr/share/metainfo" ]]; then
        # WHY: two AppStream components in one AppImage (the base image's leftover python3.12
        # component plus ours) confuses appimaged and AppImage stores.
        while IFS= read -r -d '' f; do
            safe_rm_rf "$f"
        done < <(find "${appdir}/usr/share/metainfo" -maxdepth 1 -name "python3.12*.appdata.xml" -print0 2>/dev/null)
    fi
}

remove_pycache() {
    local site="$1"
    log "removing regenerated __pycache__ directories"
    while IFS= read -r -d '' d; do
        safe_rm_rf "$d"
    done < <(find "$site" -depth -type d -name "__pycache__" -print0 2>/dev/null)
}

check_no_dangling_qt_deps() {
    local qt_dir="$1"

    log "checking for dangling Qt-to-Qt dependencies after trimming"

    local dangling=()
    local lib
    while IFS= read -r -d '' lib; do
        local ldd_out
        ldd_out="$(LD_LIBRARY_PATH="${qt_dir}/lib" ldd "$lib" 2>&1 || true)"
        local bad_line
        bad_line="$(grep -E 'libQt6[A-Za-z0-9_]*\.so[^ ]* => not found' <<<"$ldd_out" || true)"
        if [[ -n "$bad_line" ]]; then
            dangling+=("${lib}: ${bad_line}")
        fi
    done < <(find "${qt_dir}/lib" "${qt_dir}/plugins" -name "*.so*" -type f -print0 2>/dev/null)

    if [[ "${#dangling[@]}" -gt 0 ]]; then
        echo "error: trimming left dangling Qt-to-Qt dependencies (a shipped library needs another" >&2
        echo "Qt library that was trimmed away):" >&2
        local d
        for d in "${dangling[@]}"; do
            echo "  $d" >&2
        done
        exit 1
    fi
}

verify_appdir() {
    local pybin="$1" certs="$2" qt_dir="$3"

    # WHY: certifi's cacert.pem is what opt/_internal/certs.pem symlinks to; if trimming ever
    # deleted the certifi package, the symlink dangles and every catalog download fails silently.
    if [[ ! -e "$certs" ]]; then
        echo "error: SSL_CERT_FILE target is missing after trimming: $certs" >&2
        exit 1
    fi

    check_no_dangling_qt_deps "$qt_dir"

    log "running post-trim verification"

    local marker="TIPTOI_APPIMAGE_VERIFICATION_OK"
    local output
    # WHY: constructing MainWindow (not just importing the gui module) is required - gui.py catches
    # ImportError internally and guards its class bodies so a bare `import tiptoi_linux.gui` can
    # never fail, even with PySide6 entirely deleted. This exercises the model, widgets and thread
    # wiring instead. 2>&1 is required too, or a real traceback escapes uncaptured while $output
    # prints empty on failure.
    if ! output=$(QT_QPA_PLATFORM=offscreen SSL_CERT_FILE="$certs" "$pybin" - <<PYEOF 2>&1
import os

cert_path = os.environ.get("SSL_CERT_FILE", "")
assert cert_path and os.path.exists(cert_path), f"SSL_CERT_FILE missing: {cert_path!r}"

from PySide6.QtWidgets import QApplication
from tiptoi_linux.gui import MainWindow

app = QApplication([])
MainWindow()

print("${marker}")
PYEOF
    ); then
        echo "error: post-trim verification failed:" >&2
        echo "$output" >&2
        exit 1
    fi

    if ! grep -q "$marker" <<<"$output"; then
        echo "error: post-trim verification did not confirm success" >&2
        echo "$output" >&2
        exit 1
    fi

    log "post-trim verification passed"
}

main() {
    require_cmd curl
    require_cmd chmod
    require_cmd realpath
    require_cmd sha256sum
    require_cmd ldd

    mkdir -p "$CACHE_DIR"
    safe_rm_rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"

    export PIP_CACHE_DIR="${CACHE_DIR}/pip"

    log "downloading pinned base image and appimagetool"
    local base_cached appimagetool_cached
    base_cached="${CACHE_DIR}/$(basename -- "$BASE_APPIMAGE_URL")"
    appimagetool_cached="${CACHE_DIR}/$(basename -- "$APPIMAGETOOL_URL")"
    download_verified "$BASE_APPIMAGE_URL" "$base_cached" "$BASE_APPIMAGE_SHA256"
    download_verified "$APPIMAGETOOL_URL" "$appimagetool_cached" "$APPIMAGETOOL_SHA256"

    log "extracting base AppImage (no FUSE required)"
    cp -- "$base_cached" "${WORK_DIR}/base.AppImage"
    chmod +x "${WORK_DIR}/base.AppImage"
    (
        cd "$WORK_DIR"
        ./base.AppImage --appimage-extract >/dev/null
    )

    local appdir="${WORK_DIR}/squashfs-root"
    local pybin="${appdir}/opt/python3.12/bin/python3.12"
    local site="${appdir}/opt/python3.12/lib/python3.12/site-packages"
    local certs="${appdir}/opt/_internal/certs.pem"
    local qt_dir="${site}/PySide6/Qt"

    log "installing PySide6-Essentials ${PYSIDE6_VERSION}"
    # WHY: the wheel is tagged manylinux_2_34 but the base is manylinux_2_28; a plain pip install
    # is rejected on the platform tag alone even though the wheel runs fine, so the platform check
    # must be forced explicitly. Consequence: the resulting AppImage's own glibc floor becomes 2.34
    # (Ubuntu 22.04 / Debian 12 / RHEL 9 and newer), not the base image's 2.28.
    "$pybin" -m pip install \
        --platform manylinux_2_34_x86_64 \
        --only-binary=:all: \
        --target "$site" \
        --upgrade \
        "PySide6-Essentials==${PYSIDE6_VERSION}"

    log "copying project sources to avoid an in-tree setuptools build"
    # WHY: `pip install --target ... "$REPO_ROOT"` builds IN-TREE, leaving build/ and
    # *.egg-info in the repo root, where build/lib/ becomes a stale source snapshot a later
    # setuptools run can pick up by mistake. Copying only what the package needs into BUILD_DIR
    # first keeps all setuptools scratch confined there.
    local src_dir="${WORK_DIR}/src"
    mkdir -p "$src_dir"
    cp -r -- "${REPO_ROOT}/tiptoi_linux" "$src_dir/"
    cp -- "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/LICENSE" "$src_dir/"

    log "installing the project itself"
    "$pybin" -m pip install --target "$site" --upgrade --no-deps "$src_dir"

    log "assembling AppDir assets"
    install -m 755 "${SCRIPT_DIR}/AppRun" "${appdir}/AppRun"

    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "$appdir" -maxdepth 1 -name "python*.desktop" -print0 2>/dev/null)
    while IFS= read -r -d '' f; do
        safe_rm_rf "$f"
    done < <(find "$appdir" -maxdepth 1 -name "python.png" -print0 2>/dev/null)
    if [[ -d "${appdir}/usr/share/applications" ]]; then
        while IFS= read -r -d '' f; do
            safe_rm_rf "$f"
        done < <(find "${appdir}/usr/share/applications" -maxdepth 1 -name "python*.desktop" -print0 2>/dev/null)
    fi
    if [[ -d "${appdir}/usr/share/icons" ]]; then
        while IFS= read -r -d '' f; do
            safe_rm_rf "$f"
        done < <(find "${appdir}/usr/share/icons" -name "python.png" -print0 2>/dev/null)
    fi

    mkdir -p "${appdir}/usr/share/applications"
    install -m 644 "${SCRIPT_DIR}/tiptoi.desktop" "${appdir}/tiptoi.desktop"
    install -m 644 "${SCRIPT_DIR}/tiptoi.desktop" "${appdir}/usr/share/applications/tiptoi.desktop"

    mkdir -p "${appdir}/usr/share/metainfo"
    install -m 644 "${SCRIPT_DIR}/tiptoi.metainfo.xml" \
        "${appdir}/usr/share/metainfo/io.github.tiptoi_linux.tiptoi.metainfo.xml"

    log "converting icon"
    local icon_png="${WORK_DIR}/tiptoi.png"
    convert_icon "${SCRIPT_DIR}/tiptoi.svg" "$icon_png"

    mkdir -p "${appdir}/usr/share/icons/hicolor/256x256/apps"
    install -m 644 "$icon_png" "${appdir}/usr/share/icons/hicolor/256x256/apps/tiptoi.png"
    install -m 644 "$icon_png" "${appdir}/tiptoi.png"
    install -m 644 "$icon_png" "${appdir}/.DirIcon"

    trim_appdir "$appdir" "$site"
    verify_appdir "$pybin" "$certs" "$qt_dir"
    # WHY: must run after verify_appdir, not as part of trim_appdir - verification imports modules
    # and regenerates __pycache__, so sweeping it first would leave fresh caches behind.
    remove_pycache "$site"

    log "running appimagetool"
    chmod +x "$appimagetool_cached"
    # WHY: no pre-delete of OUTPUT_APPIMAGE here - it lives in REPO_ROOT, outside BUILD_DIR, and
    # every deletion in this script stays confined to BUILD_DIR; appimagetool overwrites its
    # output path itself
    export ARCH=x86_64
    "$appimagetool_cached" --appimage-extract-and-run "$appdir" "$OUTPUT_APPIMAGE"

    if [[ ! -s "$OUTPUT_APPIMAGE" ]]; then
        echo "error: appimagetool did not produce $OUTPUT_APPIMAGE" >&2
        exit 1
    fi

    log "build complete: $OUTPUT_APPIMAGE"
    # WHY: must not be the final statement as a pipeline - under `set -o pipefail`, any transient
    # hiccup in du/cut/xargs would make a completed, verified build exit non-zero.
    log "final size: $(du -h "$OUTPUT_APPIMAGE" | cut -f1)"
}

main "$@"
