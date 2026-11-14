#!/usr/bin/env bash
# Generate the benchmark dumps (idempotent; missing dumps only).
# Usage: ./gen-dumps.sh [--full]      (full adds the mixed-6g tier)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JAR="$ROOT/../heap_forge/target/heapforge-1.0-SNAPSHOT.jar"
DUMPS="$ROOT/dumps"
mkdir -p "$DUMPS"

gen() { # stem profile target_mb gen_xmx
    local stem=$1 profile=$2 target=$3 xmx=$4
    if [ -f "$DUMPS/$stem.hprof" ]; then
        echo "$stem exists ($(du -m "$DUMPS/$stem.hprof" | cut -f1) MB), skipping"
        return
    fi
    echo "generating $stem (profile=$profile target=${target}MB -Xmx$xmx)"
    /usr/bin/time -v java -Xmx"$xmx" -jar "$JAR" stress "$profile" "$target" "$DUMPS/$stem.hprof" true \
        2>&1 | grep -E 'Elapsed|Maximum resident|dump written'
}

gen mixed-512m mixed 512 2048m
gen mixed-2g mixed 2048 4608m
[ "${1:-}" = "--full" ] && gen mixed-6g mixed 6144 10240m
ls -la "$DUMPS"
