#!/usr/bin/env python3
"""A local MCP server that reports the latest Kiri release and installs it.

The tool lives in its own repository, not inside Kiri, so updating Kiri
and updating this tool stay two separate things. It speaks MCP over
stdio and needs nothing but Python's standard library:

    python3 server.py
"""

__version__ = "0.2.0"
