import os, sys, datetime, subprocess
print("CWD:", os.getcwd())
print("TIME:", datetime.datetime.now())
print("PYTHON:", sys.version)
print("FILES:", os.listdir('.'))
try:
    result = subprocess.run(['git', 'status'], capture_output=True, text=True, cwd='.')
    print("GIT:", result.stdout[:500])
except:
    print("GIT: failed")
