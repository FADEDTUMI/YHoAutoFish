#!/usr/bin/env python3
"""Generate ECDSA signature for hosts list. Migration use only."""
import argparse
import base64
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Sign a hosts list with ECDSA private key")
    parser.add_argument("--hosts", required=True, help='JSON array of hosts, e.g. \'["ip","domain"]\'')
    parser.add_argument("--key", required=True, help="Path to ECDSA private key PEM file")
    args = parser.parse_args()

    try:
        hosts = json.loads(args.hosts)
        if not isinstance(hosts, list) or not hosts:
            print("ERROR: --hosts must be a non-empty JSON array", file=sys.stderr)
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    key_path = Path(args.key)
    if not key_path.is_file():
        print(f"ERROR: Key file not found: {key_path}", file=sys.stderr)
        sys.exit(1)

    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes, serialization
    except ImportError:
        print("ERROR: pip install cryptography", file=sys.stderr)
        sys.exit(1)

    private_key_pem = key_path.read_bytes()
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)

    payload = json.dumps(hosts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.b64encode(signature).decode("ascii")

    print(f"AUTH_HOSTS_LIST={json.dumps(hosts)}")
    print(f"AUTH_HOSTS_SIGNATURE={sig_b64}")
    print()
    print("# Copy these lines to the OLD server's .env file")


if __name__ == "__main__":
    main()
