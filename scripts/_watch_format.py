#!/usr/bin/env python3
"""Format the merged gateway log stream for a human watching it live.

Reads tagged lines — ``api|…``, ``job|…``, ``bank|…`` — from stdin and prints
one readable line each. Kept out of watch.sh rather than inlined: quoting a
formatter of this size inside a shell string is how it acquires bugs nobody
can see.
"""

import json
import os
import re
import sys

MODE = os.environ.get("WATCH_MODE", "payments")
ONLY = os.environ.get("WATCH_TX", "")

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
RED, GREEN, YELLOW = "\033[31m", "\033[32m", "\033[33m"
MAGENTA, CYAN = "\033[35m", "\033[36m"

TAG = {"api": CYAN + "api " + OFF, "job": MAGENTA + "job " + OFF,
       "bank": YELLOW + "bank" + OFF}
LEVEL = {"CRITICAL": RED + BOLD, "ERROR": RED, "WARNING": YELLOW, "INFO": ""}

#: Lines that say nothing about a payment. Filtered by default because a health
#: check every two seconds buries the one line that matters.
NOISE = re.compile(
    r"GET /health|Task publish_outbox|Task reconcile_transactions"
    r"|Task apply_retention|Simulator alive|Client (dis)?connected"
    r"|celery\.worker|mingle|Connected to redis|beat:"
)
#: Lines worth making obvious even in the quiet view.
STRONG = re.compile(r"approved|captured|declined|CRYPTO_SENT|reversal|REVERSAL", re.I)


def emit(source, when, level, text, tx):
    if ONLY and str(tx) != ONLY:
        return
    if MODE == "payments" and NOISE.search(text):
        return
    colour = LEVEL.get(level, "")
    if not colour and STRONG.search(text):
        colour = GREEN
    stamp = DIM + (when or "")[11:19] + OFF
    marker = ""
    if tx not in (None, "null", ""):
        marker = " " + BOLD + "#" + str(tx) + OFF
    print("  %s %s%s %s%s%s" % (stamp, TAG.get(source, source), marker,
                                colour, text, OFF), flush=True)


def main() -> int:
    for raw in sys.stdin:
        source, _, rest = raw.rstrip("\n").partition("|")
        rest = rest.strip()
        if not rest:
            continue
        try:
            # The application logs JSON; the acquirer simulator does not.
            # Trying JSON first and falling back keeps one formatter for both
            # rather than two that drift apart.
            d = json.loads(rest)
        except json.JSONDecodeError:
            m = re.match(r"^(\S+ \S+) \[[^\]]+\] (\w+) (.*)$", rest)
            when, level, text = (
                (m.group(1).replace(" ", "T"), m.group(2), m.group(3))
                if m else ("", "INFO", rest)
            )
            # The bank names transactions by STAN, not by our id. Surfacing it
            # is what lets the two accounts be lined up by eye.
            stan = re.search(r"STAN=(\d+)", text)
            emit(source, when, level, text, stan.group(1) if stan and ONLY else None)
            continue
        if not isinstance(d, dict):
            continue
        emit(source, d.get("ts", ""), d.get("level", "INFO"),
             d.get("message", ""), d.get("transaction_id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
