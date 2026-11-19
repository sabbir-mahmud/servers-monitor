#!/bin/sh
# Builds the NexGuard Android app on EAS (the "production" profile: a signed .apk) and
# saves it to dist/, where the dashboard serves it to phones (PHONE_APP_DIR).
#
#   scripts/android-apk.sh           build on EAS, then download it
#   scripts/android-apk.sh --latest  download the newest finished build, no new build
#
# The first build asks to create the Android signing key (EAS keeps it): say yes. Keep
# using the same one, or phones can't update over the app they have.
set -eu
cd "$(dirname "$0")/.."

EAS="npx -y eas-cli@latest"

if [ "${1:-}" != "--latest" ]; then
  $EAS build --platform android --profile production --wait
fi

json="$($EAS build:list --platform android --status finished --build-profile production \
  --limit 1 --json --non-interactive)"
url="$(printf '%s' "$json" | node -e '
  let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => {
    const b = JSON.parse(s)[0];
    if (!b || !b.artifacts || !b.artifacts.buildUrl) process.exit(1);
    console.log(b.artifacts.buildUrl + " " + (b.appVersion || "0") + " " + (b.appBuildVersion || "0"));
  });')" || { echo "No finished Android build on EAS yet." >&2; exit 1; }

set -- $url
mkdir -p dist
out="dist/NexGuard-$2-$3.apk"
echo "Downloading build $2 ($3) to $out"
curl -fL --progress-bar -o "$out.part" "$1"
mv "$out.part" "$out"
echo "Done. The dashboard serves it at /d/app/NexGuard.apk."
