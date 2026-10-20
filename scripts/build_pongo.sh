#!/usr/bin/env bash
# build_pongo.sh — Build forked pongoOS under Linux with ep0 chunked transport
#
# Requirements on host or container:
#   Debian/Ubuntu packages:
#   build-essential git clang lld llvm pkg-config ca-certificates uuid-dev
#   zlib1g-dev libssl-dev flex bison gperf cmake python3 xxd libblocksruntime-dev
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
PREFIX="${BUILD_DIR}/toolchain"
PONGO_SRC="${BUILD_DIR}/pongoos-src"

mkdir -p "${PREFIX}" "${BUILD_DIR}"

# 1. Build ld64 and cctools-strip if not present
if [ ! -x "${PREFIX}/bin/arm64-apple-darwin20-ld" ]; then
    echo "[*] Building apple-libdispatch..."
    DISPATCH_SRC=$(mktemp -d /tmp/dispatch-XXXXXX)
    git clone --depth 1 https://github.com/tpoechtrager/apple-libdispatch "${DISPATCH_SRC}"
    mkdir -p "${DISPATCH_SRC}/build"
    (
        cd "${DISPATCH_SRC}/build"
        CC=clang CXX=clang++ cmake .. -DCMAKE_BUILD_TYPE=RELEASE -DCMAKE_INSTALL_PREFIX="${PREFIX}" >/dev/null
        make install -j"$(nproc)" >/dev/null
    )
    rm -rf "${DISPATCH_SRC}"

    echo "[*] Building cctools-port (ld64)..."
    CCTOOLS_SRC=$(mktemp -d /tmp/cctools-XXXXXX)
    git clone --depth 1 https://github.com/tpoechtrager/cctools-port "${CCTOOLS_SRC}"
    (
        cd "${CCTOOLS_SRC}/cctools"
        ./configure --prefix="${PREFIX}" --target=arm64-apple-darwin20 \
            --with-libdispatch="${PREFIX}" --with-libblocksruntime="${PREFIX}" >/dev/null
        make -j"$(nproc)" >/dev/null
        make install >/dev/null
    )
    rm -rf "${CCTOOLS_SRC}"
fi

LD64=""
for candidate in "${PREFIX}/bin/ld64" "${PREFIX}/bin/ld" "${PREFIX}/bin/arm64-apple-darwin20-ld"; do
    if [ -x "${candidate}" ]; then LD64="${candidate}"; break; fi
done
STRIP=""
for candidate in "${PREFIX}/bin/cctools-strip" "${PREFIX}/bin/arm64-apple-darwin20-strip"; do
    if [ -x "${candidate}" ]; then STRIP="${candidate}"; break; fi
done

if [ -z "${LD64}" ] || [ -z "${STRIP}" ]; then
    echo "[-] Error: ld64 or cctools-strip not found in ${PREFIX}/bin" >&2
    exit 1
fi

echo "[+] Using linker: ${LD64}"
echo "[+] Using strip: ${STRIP}"

# 2. Clone and patch pongoOS
if [ ! -d "${PONGO_SRC}" ]; then
    echo "[*] Cloning checkra1n/PongoOS..."
    git clone --depth 1 https://github.com/checkra1n/PongoOS "${PONGO_SRC}"
    (
        cd "${PONGO_SRC}"
        git submodule update --init --recursive
    )
fi

cd "${PONGO_SRC}"
if ! grep -q "ep0_upload_chunk" src/shell/usbloader.c; then
    echo "[*] Applying pongoos-usbloader-ep0.patch..."
    patch -p1 < "${ROOT_DIR}/patches/pongoos-usbloader-ep0.patch"
fi

# 3. Compile
echo "[*] Compiling newlib..."
make -C newlib all >/dev/null

echo "[*] Compiling pongoOS with ep0 handler..."
make clean >/dev/null
make EMBEDDED_LDFLAGS="-fuse-ld=${LD64}" STRIP="${STRIP}" \
    EMBEDDED_CFLAGS="-include ${ROOT_DIR}/compat/stdarg_shim.h -fno-lto -Wno-error=deprecated-non-prototype -Wno-error=unused-but-set-variable"

mkdir -p "${ROOT_DIR}/dist"
cp build/Pongo.bin "${ROOT_DIR}/dist/Pongo.bin"
cp build/PongoConsolidated.bin "${ROOT_DIR}/dist/PongoConsolidated.bin"

echo "[+] Done! Build artifacts in ${ROOT_DIR}/dist:"
ls -lh "${ROOT_DIR}/dist"
sha1sum "${ROOT_DIR}/dist/"*
