#!/bin/sh
# Stand-in for curl: raw.githubusercontent.com/<owner>/<repo>/<ref>/<path> -> $FAKE_REPO_ROOT/<path>
# Records the ref it was asked for. Accepts: -fsSL -o FILE URL
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "$url" >>"$FAKE_CURL_LOG"
rest="${url#https://raw.githubusercontent.com/}"
path="${rest#*/}"; path="${path#*/}"; path="${path#*/}" # owner/repo/ref/ stripped
src="$FAKE_REPO_ROOT/$path"
[ -f "$src" ] || exit 22
if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi
