#!/usr/bin/env python3
"""Run EDCMEA from a checkout: python3 edcmea.py <command> (see edcmea.sh)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from edcmea.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
