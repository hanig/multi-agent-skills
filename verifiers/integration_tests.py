#!/usr/bin/env python3
"""Run the complete repository suite in the supplied candidate merge tree.

The operator reads these bytes from the trusted target and pins their digest.
Tests and implementation are the candidate tree's bytes; this is integration
validation, not an immutable test-corpus guarantee or an OS security boundary.
"""
import os
import sys


if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
