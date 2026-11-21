#!/usr/bin/env python3
"""The Kiri MCP server: run it over stdio, with nothing else on the command line.

    python3 server.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kirimcp.server import main

if __name__ == "__main__":
    main()
