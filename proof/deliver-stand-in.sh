#!/usr/bin/env bash
# Public stand-in for a private delivery.
#
# Downloads REF_URL into a local directory, so the install that follows reads a
# local file exactly as it will when a candidate arrives privately, by an
# authenticated Actions artifact download or a private copy onto a machine we
# control. It checks nothing itself: the install verifies the sha256 of the
# local file before extracting it.
#
# Usage: deliver-stand-in.sh <directory> github <GITHUB_ENV path>
#        deliver-stand-in.sh <directory> shell  <sourceable env file>
# Writes REF_FILE and the transfer record (TRANSFER_KIND, TRANSFER_REF,
# TRANSFER_NOTE) in the requested format.
set -euo pipefail
dir="$1"
format="$2"
out="$3"
[ -n "${REF_URL:-}" ] || { echo "::error::deliver-stand-in needs REF_URL" >&2; exit 1; }
mkdir -p "$dir"
file="$dir/$(basename "$REF_URL")"
curl -fsSL --retry 3 -o "$file" "$REF_URL"

path="$file"
if command -v cygpath > /dev/null 2>&1; then path="$(cygpath -w "$file")"; fi
kind=public-download-stand-in
note="Downloaded inside this job from a public URL to stand in for a private delivery. The install read this local file, never the URL."
case "$format" in
  github)
    {
      printf 'REF_FILE=%s\n' "$path"
      printf 'TRANSFER_KIND=%s\n' "$kind"
      printf 'TRANSFER_REF=%s\n' "$REF_URL"
      printf 'TRANSFER_NOTE=%s\n' "$note"
    } >> "$out"
    ;;
  shell)
    {
      printf 'REF_FILE=%q\n' "$path"
      printf 'TRANSFER_KIND=%q\n' "$kind"
      printf 'TRANSFER_REF=%q\n' "$REF_URL"
      printf 'TRANSFER_NOTE=%q\n' "$note"
    } >> "$out"
    ;;
  *)
    echo "::error::unknown output format $format" >&2
    exit 1
    ;;
esac
echo "delivered $(basename "$file") to $path ($(wc -c < "$file" | tr -d ' ') bytes)"
