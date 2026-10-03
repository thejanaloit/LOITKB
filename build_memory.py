"""Deprecated entry point. Same as: py -3 -m loitkb sync"""
import sys

from loitkb.cli import main

if __name__ == "__main__":
    sys.exit(main(["sync", *sys.argv[1:]]))
