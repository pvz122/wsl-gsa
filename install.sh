#!/bin/sh
set -eu
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'Python 3.9+ is required in WSL. Install it with your distribution package manager.' >&2
    exit 1
}
exec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/src/install.py" "$@"
