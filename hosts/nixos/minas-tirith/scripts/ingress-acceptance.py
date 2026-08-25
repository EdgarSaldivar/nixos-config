#!/usr/bin/env python3
"""Verify Traefik ingress against a recorded pre-cutover baseline."""

import sys

from ingress_acceptance.cli import main


if __name__ == "__main__":
    sys.exit(main())
