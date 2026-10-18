# run_all.py -- run every tests/test_*.py in its own process and sum up.
#   python3 tests/run_all.py
# Skips (e.g. "skipped: original code retired") are counted separately and
# do not fail the run.
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
tot_p = tot_f = tot_s = 0
for name in sorted(os.listdir(HERE)):
    if not (name.startswith("test_") and name.endswith(".py")):
        continue
    p = subprocess.run([sys.executable, os.path.join(HERE, name)],
                       capture_output=True, text=True, cwd=HERE, timeout=600)
    m = re.findall(r"^(\d+) passed, (\d+) failed(?:, (\d+) skipped)?", p.stdout, re.M)
    if m:
        ps, fs, ss = int(m[-1][0]), int(m[-1][1]), int(m[-1][2] or 0)
    else:                       # repo tests with their own summary line
        ps, fs, ss = (1, 0, 0) if p.returncode == 0 else (0, 1, 0)
    if p.returncode and not fs:
        fs = 1
    tot_p += ps
    tot_f += fs
    tot_s += ss
    print("%-28s %3d passed %2d failed %2d skipped" % (name, ps, fs, ss))
    if fs:
        print(p.stdout[-3000:], p.stderr[-3000:])
    for line in p.stdout.splitlines():
        if line.strip().startswith("skip "):
            print("    " + line.strip())
print("\nTOTAL %d passed, %d failed, %d skipped" % (tot_p, tot_f, tot_s))
sys.exit(1 if tot_f else 0)
