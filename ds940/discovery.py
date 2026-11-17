"""Bonjour / mDNS dynamic discovery for Brother DS-940DW."""

from __future__ import annotations

import logging
import re
import socket
import subprocess

LOG = logging.getLogger(__name__)

KNOWN_HOSTNAMES = ["BR5CF370DC7026.local", "brother-ds940dw.local"]


def discover_scanner_ip(preferred_ip: str | None = None) -> str | None:
    """Resolve current scanner IP via socket check, known mDNS hostnames, or Bonjour DNS-SD."""
    # 1. Quick test of preferred IP if given
    if preferred_ip:
        try:
            with socket.create_connection((preferred_ip, 8080), timeout=1.0):
                return preferred_ip
        except (OSError, socket.timeout):
            pass

    # 2. Check known hardware mDNS hostnames
    for hostname in KNOWN_HOSTNAMES:
        try:
            ip = socket.gethostbyname(hostname)
            if ip:
                return ip
        except (socket.gaierror, OSError):
            pass

    # 3. Query local Bonjour zone for _uscan._tcp service
    try:
        proc = subprocess.Popen(
            ["dns-sd", "-Z", "_uscan._tcp", "local."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()

        match = re.search(r"SRV\s+\d+\s+\d+\s+\d+\s+([A-Za-z0-9\-\._]+)", stdout)
        if match:
            target = match.group(1).rstrip(".")
            return socket.gethostbyname(target)
    except Exception as err:
        LOG.debug("Bonjour DNS-SD discovery failed: %s", err)

    return None
