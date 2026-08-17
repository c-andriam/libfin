#!/usr/bin/env python3
"""
Mock acquirer for simulation runs.

A simulator that approves everything only ever exercises the happy path, which
is the one least likely to lose money. This one can decline, stay silent, refuse
a reversal and answer echoes, so the failure branches of the gateway — the ones
that decide whether a cardholder gets refunded — are actually reachable.

Behaviour is selected by the card number, so a test or a manual curl picks a
scenario just by choosing a PAN:

    4111111111111111  approve
    4000000000000002  decline (51, insufficient funds)
    4000000000000010  decline (05, do not honour)
    4000000000000028  no response at all (authorisation timeout)
    4000000000000036  approve, then refuse the reversal
    4000000000000044  approve, but answer slowly (SIM_SLOW_DELAY seconds)

Every scenario is also reachable through ISO 8583 as usual; nothing here is
special-cased inside the gateway.
"""

import asyncio
import logging
import os
import ssl
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from libfin import iso8583  # noqa: E402
from libfin.network.server import Iso8583Server  # noqa: E402

logging.basicConfig(
    level=os.environ.get("SIM_LOG_LEVEL", "INFO"),
    format="%(asctime)s [bank-sim] %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("bank-simulator")

APPROVED = "00"

# Match the gateway's dialect so the simulator exercises the same encoding the
# acquirer will see, not merely the library defaults.
HEX_BITMAP = os.environ.get("SIM_HEX_BITMAP", "").lower() in ("1", "true", "yes")
ENCODING = os.environ.get("SIM_ENCODING", "latin_1")


def dumps(message: dict) -> bytes:
    return iso8583.dumps(message, encoding=ENCODING, hex_bitmap=HEX_BITMAP)


def loads(raw: bytes) -> dict:
    return iso8583.loads(raw, encoding=ENCODING, hex_bitmap=HEX_BITMAP)


# PAN -> (action code, behaviour)
SCENARIOS = {
    "4000000000000002": ("51", "decline"),
    "4000000000000010": ("05", "decline"),
    "4000000000000028": (None, "silent"),
    "4000000000000036": (APPROVED, "reject_reversal"),
    "4000000000000044": (APPROVED, "slow"),
}

SLOW_DELAY = float(os.environ.get("SIM_SLOW_DELAY", "15"))

#: STANs whose reversal must be refused, populated when a 0200 is approved.
_reject_reversal_stans: set = set()
_auth_count = 0


def _auth_code(stan: str) -> str:
    return f"{(int(stan) * 7919) % 1000000:06d}"


async def _handle_authorization(msg: dict) -> bytes:
    global _auth_count
    _auth_count += 1

    pan = str(msg.get("DE2", ""))
    stan = str(msg.get("DE11", "")).zfill(6)
    amount = msg.get("DE4", 0)
    action_code, behaviour = SCENARIOS.get(pan, (APPROVED, "approve"))

    if behaviour == "silent":
        LOGGER.warning(f"STAN={stan}: staying silent on purpose (timeout scenario).")
        return b""

    if behaviour == "slow":
        LOGGER.info(f"STAN={stan}: delaying the response by {SLOW_DELAY}s.")
        await asyncio.sleep(SLOW_DELAY)

    if behaviour == "reject_reversal":
        _reject_reversal_stans.add(stan)

    response = dict(msg)
    response["MTI"] = "0210"
    response["DE39"] = action_code
    if action_code == APPROVED:
        response["DE38"] = _auth_code(stan)
        LOGGER.info(f"STAN={stan}: approved {amount} minor units.")
    else:
        LOGGER.info(f"STAN={stan}: declined with {action_code}.")

    # An acquirer echoes the retrieval reference number back untouched.
    return dumps(response)


async def _handle_reversal(msg: dict) -> bytes:
    stan = str(msg.get("DE11", "")).zfill(6)
    original = str(msg.get("DE90", ""))
    original_stan = original[4:10] if len(original) >= 10 else ""
    pan = str(msg.get("DE2", ""))

    response = dict(msg)
    response["MTI"] = "0410"

    if "*" in pan or not pan.isdigit():
        # Exactly what a real acquirer does with a masked PAN, and the reason
        # the gateway must keep the real one available for reversals.
        LOGGER.error(f"STAN={stan}: reversal carries an unusable PAN {pan!r}; rejecting.")
        response["DE39"] = "30"  # format error
    elif original_stan in _reject_reversal_stans:
        LOGGER.warning(f"STAN={stan}: refusing the reversal of {original_stan} (scenario).")
        response["DE39"] = "96"  # system malfunction
    elif not original_stan:
        LOGGER.error(f"STAN={stan}: reversal without usable original data elements.")
        response["DE39"] = "30"
    else:
        LOGGER.info(f"STAN={stan}: reversal of {original_stan} accepted.")
        response["DE39"] = APPROVED

    return dumps(response)


async def _handle_echo(msg: dict) -> bytes:
    response = dict(msg)
    response["MTI"] = "0810"
    response["DE39"] = APPROVED
    LOGGER.debug(f"Echo answered (STAN={msg.get('DE11')}).")
    return dumps(response)


async def handle_message(msg_bytes: bytes) -> bytes:
    try:
        msg = loads(msg_bytes)
    except Exception as exc:
        LOGGER.error(f"Undecodable message: {exc}")
        return b""

    mti = str(msg.get("MTI", ""))
    LOGGER.debug(f"Received MTI={mti} STAN={msg.get('DE11')}")

    try:
        if mti == "0200":
            return await _handle_authorization(msg)
        if mti == "0400":
            return await _handle_reversal(msg)
        if mti == "0800":
            return await _handle_echo(msg)
    except Exception as exc:
        LOGGER.exception(f"Failed to build a response for MTI={mti}: {exc}")
        return b""

    LOGGER.warning(f"Unsupported MTI={mti}; ignoring.")
    return b""


async def _echo_probe(server_ready: asyncio.Event) -> None:
    """Log a heartbeat so operators can see the simulator is alive."""
    await server_ready.wait()
    while True:
        await asyncio.sleep(300)
        LOGGER.info(
            f"Simulator alive since start; {_auth_count} authorisation(s) handled "
            f"as of {datetime.now(timezone.utc).isoformat(timespec='seconds')}."
        )


def _build_ssl_context():
    """Serve over TLS when asked.

    Production requires BANK_USE_TLS=true, so a plaintext-only simulator leaves
    the encrypted acquirer path — certificates, handshake, framing over TLS —
    completely unexercised until the day it faces a real bank.
    """
    if os.environ.get("SIM_USE_TLS", "").lower() not in ("1", "true", "yes"):
        return None

    cert = os.environ.get("SIM_TLS_CERT", "/certs/server.crt")
    key = os.environ.get("SIM_TLS_KEY", "/certs/server.key")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    LOGGER.info(f"TLS enabled using {cert}")
    return context


async def main() -> None:
    host = os.environ.get("SIM_HOST", "0.0.0.0")
    port = int(os.environ.get("SIM_PORT", "9000"))

    server = Iso8583Server(
        host, port, handle_message, length_header_size=2, ssl_context=_build_ssl_context()
    )
    LOGGER.info(f"Mock acquirer listening on {host}:{port}")
    LOGGER.info("Scenario cards: " + ", ".join(f"{p}={b[1]}" for p, b in SCENARIOS.items()))
    await server.start()

    ready = asyncio.Event()
    ready.set()
    await _echo_probe(ready)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER.info("Simulator stopped.")
