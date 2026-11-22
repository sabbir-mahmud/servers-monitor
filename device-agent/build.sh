#!/bin/sh
# Build the NexGuard device agent for every supported system into dist/, with checksums.
#   ./build.sh            version from VERSION
#   VERSION=1.2.3 ./build.sh
#   DIST=/some/folder ./build.sh   into another folder (the dashboard rebuilds this way)
set -eu
cd "$(dirname "$0")"
VERSION="${VERSION:-$(cat VERSION)}"
DIST="${DIST:-dist}"
rm -rf "$DIST" && mkdir -p "$DIST"
for target in darwin/arm64 darwin/amd64 linux/amd64 linux/arm64 windows/amd64 windows/arm64; do
  os="${target%/*}"; arch="${target#*/}"
  out="$DIST/nexguard-$os-$arch"; [ "$os" = windows ] && out="$out.exe"
  # No cgo: one static file per system, nothing to install alongside it.
  CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" go build -trimpath \
    -ldflags "-s -w -X main.Version=$VERSION" -o "$out" .
  echo "built $out"
done
cd "$DIST"
if command -v sha256sum >/dev/null; then sha256sum nexguard-*; else shasum -a 256 nexguard-*; fi > SHA256SUMS
echo "checksums in $DIST/SHA256SUMS"
