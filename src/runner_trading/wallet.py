"""Create a dedicated signer when the wallet owner runs the setup command."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from solders.keypair import Keypair


def create(path: Path) -> str:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Use a private directory owned by the current user, with mode 0700")
    signer = Keypair()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(list(bytes(signer)), stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise
    return str(signer.pubkey())


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Dash's dedicated Solana wallet")
    parser.add_argument("path", type=Path, help="New signer file in a private directory")
    args = parser.parse_args()
    try:
        address = create(args.path)
    except (OSError, ValueError):
        parser.exit(1, "Choose a new file in a private directory owned by this user.\n")
    print(f"Dash wallet: {address}")
    print("Fund this address with USDC and SOL for network fees.")


if __name__ == "__main__":
    main()
