#!/usr/bin/env python3
"""Debug script to check environment."""
import os
import sys
import datetime

print(f"CWD: {os.getcwd()}")
print(f"TIME: {datetime.datetime.now()}")
print(f"PYTHON: {sys.version}")
print(f"FILES: {os.listdir('.')}")
