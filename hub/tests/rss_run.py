#!/usr/bin/env python3
"""PROTOTYPE helper: runs a command and prints wall seconds and the peak RSS of the command's process tree (the largest
descendant), like `/usr/bin/time -v`, for images that have python3 but no GNU time. usage: rss_run.py <cmd> [args...]"""
import resource
import subprocess
import sys
import time

t = time.time()
rc = subprocess.run(sys.argv[1:]).returncode
wall = time.time() - t
kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(f"RSS_RUN wall_s={wall:.1f} peak_rss_mb={kb / 1024:.0f} exit={rc} cmd={' '.join(sys.argv[1:3])}", flush=True)
sys.exit(rc)
