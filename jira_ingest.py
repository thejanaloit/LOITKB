"""Deprecated entry point. Same as: py -3 -m loitkb jira"""
import sys

from loitkb.cli import main

if __name__ == "__main__":
    sys.exit(main(["jira", *sys.argv[1:]]))
