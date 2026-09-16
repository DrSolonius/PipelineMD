#!/usr/bin/env python3
"""OpenSSH askpass: stdout is read by SSH, never by application logging."""
import os
import sys

if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]).lower()
    if "password" not in prompt:
        raise SystemExit(1)
    sys.stdout.write(os.environ.get("MD_SSH_PASSWORD", "") + "\n")
