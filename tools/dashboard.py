"""Launch the read-only panel with explicit deployment paths."""
import argparse
import json
import os
from pathlib import Path
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config',type=Path,required=True)
args,remaining=parser.parse_known_args()
config=json.loads(args.config.read_text())
for key,field in [('HLS_RUN_ROOT','run_root'),('HLS_OUTPUT_ROOT','output_root'),('HLS_NAS_SOURCE','nas_source')]:
    os.environ[key]=config[field]
root=Path(__file__).resolve().parents[1]
os.execv(sys.executable,[sys.executable,'-u',str(root/'watch_hls_progress.py'),*remaining])
