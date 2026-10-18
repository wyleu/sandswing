# run_all.py -- run every tests/test_*.py in its own process and sum up.
#   python3 tests/run_all.py
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
tot_p = tot_f = 0
for name in sorted(os.listdir(HERE)):
    if not (name.startswith("test_") and name.endswith(".py")):
        continue
    p = subprocess.run([sys.executable, os.path.join(HERE, name)],
                       capture_output=True, text=True, cwd=HERE, timeout=600)
    m = re.findall(r"^(\d+) passed, (\d+) failed", p.stdout, re.M)
    if m:
        ps, fs = int(m[-1][0]), int(m[-1][1])
    else:                       # repo tests with their own summary line
        ps, fs = (1, 0) if p.returncode == 0 else (0, 1)
    if p.returncode and not fs:
        fs = 1
    tot_p += ps
    tot_f += fs
    print("%-28s %3d passed %2d failed" % (name, ps, fs))
    if fs:
        print(p.stdout[-3000:], p.stderr[-3000:])
print("\nTOTAL %d passed, %d failed" % (tot_p, tot_f))
sys.exit(1 if tot_f else 0)
