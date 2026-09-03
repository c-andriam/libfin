#!/usr/bin/env python3
"""
Serve the payment form, optionally relaying the API from the same origin.

Two modes:

*Static* (default). The page is served here and talks to the gateway directly,
on another origin. Simple, but the browser then holds the API key and CORS must
be configured. Fine for simulation, not for real cards.

*Relay* (``--gateway URL``). Requests to /health, /pay and /transaction/{id}
are forwarded to the gateway with the API key added server-side, from the
environment. The browser sees one origin and never sees the key, so CORS stops
being involved at all.

The relay exists so development matches the production shape, where Nginx does
the same job. It is not itself production infrastructure: single-threaded-ish
Python is no substitute for a real proxy, and it terminates no TLS.

Usage:
    python frontend/serve.py
    python frontend/serve.py --gateway https://localhost:8443 --insecure
    python frontend/serve.py --port 8080 --host 0.0.0.0

Environment:
    GATEWAY_API_KEY   injected as X-API-Key on relayed requests. Read from the
                      environment rather than a flag: command lines are visible
                      to every process on the machine via `ps`.
"""

import argparse
import functools
import http.server
import json
import os
import pathlib
import re
import socketserver
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent

#: Exactly what the form needs. An allowlist rather than a catch-all: a relay
#: that forwards anything would happily expose /health/ready, whose component
#: names are a map of the estate.
RELAYED = (
    ("GET", re.compile(r"^/health$")),
    ("POST", re.compile(r"^/pay$")),
    ("GET", re.compile(r"^/transaction/\d+$")),
    # Payment links. The relay adds the API key, so the payer's browser can ask
    # what a link charges without holding a credential of its own.
    ("POST", re.compile(r"^/link$")),
    ("GET", re.compile(r"^/link/[A-Za-z0-9_-]{16,64}$")),
    # The management console. Relayed, but see UNKEYED below: these are the
    # routes that list destination wallets, switch links off and delete them.
    ("GET", re.compile(r"^/links$")),
    ("PATCH", re.compile(r"^/links/\d+$")),
    ("DELETE", re.compile(r"^/links/\d+$")),
)

#: Client headers worth carrying through. Everything else — cookies above all —
#: is dropped rather than forwarded.
# X-API-Key is forwarded so an operator can authenticate to the management
# routes from the browser, where the relay deliberately supplies nothing.
FORWARD_REQUEST_HEADERS = (
    "Content-Type", "Idempotency-Key", "X-Correlation-Id", "X-API-Key",
)
FORWARD_RESPONSE_HEADERS = ("Content-Type", "X-Correlation-Id", "Retry-After")

#: Longer than the gateway's own BANK_TIMEOUT_SEC so the API gets to answer
#: properly instead of the relay cutting it off mid-authorisation.
RELAY_TIMEOUT = 45


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, never cached, plus an optional relay to the gateway."""

    server_version = "libfin-frontend"
    sys_version = ""

    # Set by main(); None means static-only.
    gateway = None
    #: Serve only what a payer following a link needs. Set for the origin that
    #: faces the public; left off for the merchant's own, private one.
    payer_only = False
    api_key = ""
    ssl_context = None

    # ── Static ──────────────────────────────────────────────────────────────

    def end_headers(self):
        # A card form served from a stale cache is a card form whose validation
        # rules no longer match the gateway's.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    # ── Relay ───────────────────────────────────────────────────────────────

    def _relays(self, method):
        if not self.gateway:
            return False
        path = self.path.split("?", 1)[0]
        return any(m == method and p.match(path) for m, p in RELAYED)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    #: Routes the relay forwards but never authenticates on the caller's behalf.
    MANAGEMENT = re.compile(r"^/links(/\d+)?$")

    @classmethod
    def _is_management(cls, path):
        return bool(cls.MANAGEMENT.match(path))

    def _relay(self, method):
        """Forward one request upstream, adding the API key on the way.

        Except for the management routes — see the comment at the injection.
        """
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        path = urllib.parse.urlsplit(self.path).path

        request = urllib.request.Request(
            self.gateway + self.path,
            data=body,
            method=method,
        )
        for name in FORWARD_REQUEST_HEADERS:
            value = self.headers.get(name)
            if value:
                request.add_header(name, value)
        # The whole point of the relay: the key lives here, not in the browser.
        # With one exception. Injecting it into the management routes would
        # make listing, disabling and deleting links available to anyone who
        # can reach this origin — the relay would be handing out an authority
        # the caller never proved. For those, whatever the browser sent is
        # forwarded unchanged, so an operator must supply the key themselves.
        if self.api_key and not self._is_management(path):
            request.add_header("X-API-Key", self.api_key)

        try:
            with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT,
                                        context=self.ssl_context) as upstream:
                status, headers, payload = upstream.status, upstream.headers, upstream.read()
        except urllib.error.HTTPError as exc:
            # A decline is a 400 and a rate limit a 429: upstream refusals are
            # answers, and must reach the page unchanged.
            status, headers, payload = exc.code, exc.headers, exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            self.log_message("relay to %s failed: %s", self.gateway, reason)
            return self._json(502, {"detail": f"Gateway unreachable through the relay: {reason}"})
        except TimeoutError:
            # Same reasoning as the gateway's own 504: the debit's fate is
            # unknown, so the page must not invite a retry.
            return self._json(504, {"detail": "Gateway did not respond in time."})

        self.send_response(status)
        for name in FORWARD_RESPONSE_HEADERS:
            if headers.get(name):
                self.send_header(name, headers[name])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ── Dispatch ────────────────────────────────────────────────────────────

    #: Everything a payer following a link needs, and nothing else. The
    #: merchant console is a separate concern served from a private origin;
    #: hiding its buttons is not access control, and a payer who types the
    #: bare hostname would otherwise land straight on it.
    PAYER_PATHS = re.compile(
        r"^/(payment\.html|result\.html|link-required\.html|favicon\.ico|assets/.+)$"
    )

    def _forbidden_for_payers(self, method):
        if not self.payer_only:
            return False
        path = urllib.parse.urlsplit(self.path).path
        if method == "GET":
            return not (self.PAYER_PATHS.match(path) or self._relays("GET"))
        # Minting links is the merchant's act. Left open on a public origin it
        # is an unauthenticated money-movement API, because this relay supplies
        # the API key that the caller does not have.
        return path == "/link"

    def do_GET(self):
        if self._forbidden_for_payers("GET"):
            # The root of a public origin should not answer with a bare 404:
            # it reads as an outage, and the visitor is usually someone whose
            # link got truncated rather than someone probing. Say what is
            # missing without disclosing that a merchant console exists here.
            if urllib.parse.urlsplit(self.path).path == "/":
                self.path = "/link-required.html"
                return super().do_GET()
            return self._json(404, {"detail": "Not found."})
        if self._relays("GET"):
            return self._relay("GET")
        super().do_GET()

    def do_HEAD(self):
        if self._relays("GET"):
            return self._json(405, {"detail": "Method not allowed."})
        super().do_HEAD()

    def do_PATCH(self):
        if self._relays("PATCH"):
            return self._relay("PATCH")
        return self._json(405, {"detail": "Method not allowed."})

    def do_DELETE(self):
        if self._relays("DELETE"):
            return self._relay("DELETE")
        return self._json(405, {"detail": "Method not allowed."})

    def do_POST(self):
        if self._forbidden_for_payers("POST"):
            return self._json(404, {"detail": "Not found."})
        if self._relays("POST"):
            return self._relay("POST")
        self._json(404, {"detail": "Not found."})


class Server(socketserver.ThreadingTCPServer):
    """Threaded: a payment waiting on the acquirer must not block the page."""

    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--port", type=int, default=5173, help="port to listen on (default: 5173)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default: 127.0.0.1, loopback only)")
    parser.add_argument("--gateway", default="",
                        help="relay /health, /pay and /transaction to this base URL")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification when relaying (self-signed simulation cert only)")
    parser.add_argument("--payer-only", action="store_true",
                        help="serve only the payment and confirmation pages: no link "
                             "configuration, and no link creation. Use this for any "
                             "origin a payer can reach.")
    args = parser.parse_args()

    Handler.gateway = args.gateway.rstrip("/")
    Handler.api_key = os.environ.get("GATEWAY_API_KEY", "")
    Handler.payer_only = args.payer_only

    if Handler.gateway and args.insecure:
        Handler.ssl_context = ssl._create_unverified_context()

    handler = functools.partial(Handler, directory=str(ROOT))
    origin = f"http://{args.host}:{args.port}"

    try:
        with Server((args.host, args.port), handler) as httpd:
            print(f"  Formulaire     : {origin}")
            print(f"  Racine servie  : {ROOT}")
            if Handler.gateway:
                print(f"  Relais         : {Handler.gateway} (/health, /pay, /transaction/{{id}})")
            if Handler.payer_only:
                print("  Mode           : payeur — la configuration des liens n'est pas servie")
                if Handler.api_key:
                    print("  Clé d'API      : injectée côté serveur, absente du navigateur")
                else:
                    print("  Clé d'API      : GATEWAY_API_KEY non défini — le gateway répondra 401")
                if args.insecure:
                    print("  TLS            : vérification désactivée (--insecure)")
                print("  CORS           : sans objet, une seule origine")
            else:
                print("  Mode           : statique — la page appelle le gateway directement")
                print(f"  À autoriser    : CORS_ORIGINS={origin}")
            print("  Ctrl+C pour arrêter.\n")
            httpd.serve_forever()
    except OSError as exc:
        print(f"  Impossible d'écouter sur {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n  Arrêté.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
