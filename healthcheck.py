#!/usr/bin/env python3

# just needed for the CLI, as it selects the website to check, passes it as an sysarg and we run the core checker

import importlib
import sys

import healthcheck_core

SITES = ["sothebys", "christies", "phillips"]


def run():
    if len(sys.argv) < 2 or sys.argv[1] not in SITES:
        print(__doc__)
        print("Available sites:", ", ".join(SITES))
        return 2
    site = sys.argv.pop(1)
    healthcheck_core.use_site(importlib.import_module("sites." + site))
    sys.argv[0] = f"healthcheck.py {site}"
    return healthcheck_core.main()


if __name__ == "__main__":
    sys.exit(run())