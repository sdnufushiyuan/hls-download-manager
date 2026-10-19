#!/bin/sh
set -eu
HLS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$HLS_DIR/.venv/bin/python" "$HLS_DIR/download_hls_optram.py" "$@"
