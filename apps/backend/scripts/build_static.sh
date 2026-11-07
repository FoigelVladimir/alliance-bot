#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
backend=apps/backend
platform="$(uname -s)-$(uname -m)"
entry="$(awk -v platform="$platform" '$2 == platform { print; exit }' "$backend/assets/tailwind/cli.lock")"
if [[ -z "$entry" ]]; then
    echo "Tailwind CLI is not pinned for $platform" >&2
    exit 1
fi
read -r version _ asset checksum <<< "$entry"
cache="data/tools/tailwind/$version"
mkdir -p "$cache" "$backend/static/css"
binary="$cache/$asset"
verify() {
    [[ "$(shasum -a 256 "$1" | awk '{print $1}')" == "$checksum" ]]
}
if [[ ! -f "$binary" ]] || ! verify "$binary"; then
    download="$(mktemp "$cache/download.XXXXXX")"
    trap 'rm -f "$download"' EXIT
    curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/$version/$asset" -o "$download"
    verify "$download" || { echo "Tailwind CLI checksum mismatch" >&2; exit 1; }
    chmod 755 "$download"
    mv "$download" "$binary"
fi
"$binary" --help | head -n 1 | grep -F "$version" >/dev/null
"$binary" -i "$backend/assets/tailwind/input.css" -o "$backend/static/css/app.css" --minify
