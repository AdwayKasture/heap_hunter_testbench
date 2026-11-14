#!/usr/bin/env bash
# Build heap_hunter (ReleaseFast) and heapforge, download Eclipse MAT 1.17.0.
# Idempotent: skips steps whose outputs already exist unless --force is given.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }; }
need zig; need mvn; need java; need unzip; need curl

echo "== heap_hunter: zig build (ReleaseFast)"
if [ "$FORCE" -eq 1 ] || [ ! -x "$ROOT/../heap_hunter/zig-out/bin/hunt" ]; then
    (cd "$ROOT/../heap_hunter" && zig build -Doptimize=ReleaseFast)
else
    echo "   hunt binary already present (use --force to rebuild)"
fi
"$ROOT/../heap_hunter/zig-out/bin/hunt" --version

echo "== heapforge: mvn package"
if [ "$FORCE" -eq 1 ] || [ ! -f "$ROOT/../heap_forge/target/heapforge-1.0-SNAPSHOT.jar" ]; then
    (cd "$ROOT/../heap_forge" && mvn -q clean package -DskipTests)
else
    echo "   heapforge jar already present (use --force to rebuild)"
fi

echo "== Eclipse MAT 1.17.0"
if [ "$FORCE" -eq 1 ] || [ ! -x "$ROOT/mat/ParseHeapDump.sh" ]; then
    URL="https://download.eclipse.org/mat/1.17.0/rcp/MemoryAnalyzer-1.17.0.20260601-linux.gtk.x86_64.zip"
    curl -f --retry 3 -o "$ROOT/mat-1.17.0.zip" "$URL"
    rm -rf "$ROOT/mat"
    unzip -q -o "$ROOT/mat-1.17.0.zip" -d "$ROOT"
    echo "   MAT installed at $ROOT/mat"
else
    echo "   MAT already installed (use --force to re-download)"
fi

echo "== smoke tests"
SMOKE="$(mktemp -d)"
trap 'rm -rf "$SMOKE"' EXIT
java -Xmx512m -jar "$ROOT/../heap_forge/target/heapforge-1.0-SNAPSHOT.jar" \
    stress mixed 16 "$SMOKE/smoke.hprof" true >/dev/null
"$ROOT/../heap_hunter/zig-out/bin/hunt" index "$SMOKE/smoke.hprof" --index-dir "$SMOKE/idx" >/dev/null
"$ROOT/mat/ParseHeapDump.sh" "$SMOKE/smoke.hprof" "$ROOT/mat-report/bench_extract.xml" -vmargs -Xmx512m >/dev/null 2>&1
test -f "$SMOKE/smoke.hprof.index" && test -f "$(find "$SMOKE" -name 'Histogram2.csv' | head -1)"
echo "   smoke dump indexed by hunt and parsed by MAT OK"

echo "setup complete"
