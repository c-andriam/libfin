#!/usr/bin/env python3
"""
Generate a throwaway wallet for testnet rehearsals.

Kept out of git (see .gitignore) and written 0600, because it holds a private
key — worthless on any real network, but treating testnet keys carelessly is
how the habit forms.
"""

import json
import pathlib
import sys

from eth_account import Account

FAUCETS = (
    "https://sepolia-faucet.pk910.de                (gas, browser proof-of-work)",
    "https://www.alchemy.com/faucets/ethereum-sepolia  (gas, account required)",
    "https://faucets.chain.link/sepolia             (test LINK, the ERC-20 leg)",
)


def main() -> int:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "testnet-wallet.json")

    if path.exists():
        address = json.loads(path.read_text())["address"]
        print(f"  Existing wallet: {address}")
    else:
        account = Account.create()
        path.write_text(
            json.dumps(
                {
                    "note": "TESTNET ONLY. Never fund this address on a real network.",
                    "address": account.address,
                    "private_key": account.key.hex(),
                },
                indent=2,
            )
        )
        path.chmod(0o600)
        print(f"  Wallet written to {path} (0600)")
        print(f"  Address to fund: {account.address}")

    print()
    print("  Fund it with both gas and tokens, then run: make prodtest")
    for faucet in FAUCETS:
        print(f"    {faucet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
