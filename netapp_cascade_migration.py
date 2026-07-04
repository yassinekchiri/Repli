#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backward-compatible entry point.

The tool now lives in the netapp_migration package (engine + REST/SSH
transports + CLI + FastAPI). This wrapper keeps the historical command
working:

    python3 netapp_cascade_migration.py --action create ...

See README.md for the full CLI reference and the API server setup.
"""

import sys

from netapp_migration.interfaces.cli import main

if __name__ == "__main__":
    sys.exit(main())
