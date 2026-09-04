#!/usr/bin/env bash
#
#  Put the gateway on a public hostname, from a machine that has no public IP.
#
#  Why this exists: the host running the gateway sits behind carrier-grade NAT
#  — a traceroute reaches a second private address inside the ISP's network
#  before any public hop — so no inbound port can ever reach it, and forwarding
#  ports on the local router cannot help. A tunnel works because the connection
#  is established outbound, which that NAT permits.
#
#  Everything here is idempotent. Run it on a freshly cloned checkout, or run it
#  again on a machine that already has half of it; each step checks before it
#  acts. The only step that needs a human is the browser authorisation, and it
#  is skipped when a certificate is already present.
#
#      scripts/tunnel_setup.sh            # configure, then print how to run it
#      scripts/tunnel_setup.sh --run      # configure, then run in the foreground
#      scripts/tunnel_setup.sh --status   # what is configured and what is live
#
#  Overridable:
#      TUNNEL_NAME      default libfin-pay
#      TUNNEL_HOSTNAME  default pay.cookshare.me
#      TUNNEL_ORIGIN    default https://localhost:${HTTPS_PORT:-8444}
#
set -uo pipefail
cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; DIM=$'\033[2m'; OFF=$'\033[0m'
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'

say()  { echo "${BLUE}▸${OFF} $*"; }
ok()   { echo "  ${GREEN}✔${OFF} $*"; }
warn() { echo "  ${YELLOW}!${OFF} $*"; }
bad()  { echo "  ${RED}✘${OFF} $*"; }
note() { echo "  ${DIM}$*${OFF}"; }

TUNNEL_NAME="${TUNNEL_NAME:-libfin-pay}"
TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-pay.cookshare.me}"

# The origin is Nginx, not the development relay. Nginx already serves the
# pages and relays the API on one TLS origin, so pointing the tunnel at it
# means one process to keep alive instead of two — and the browser sees the
# same origin in production as it does locally.
ENV_FILE="${GATEWAY_ENV_FILE:-.env.prod}"
HTTPS_PORT="${HTTPS_PORT:-$(sed -n 's/^HTTPS_PORT=//p' "${ENV_FILE}" 2>/dev/null | tr -d '\r' | tail -1)}"
HTTPS_PORT="${HTTPS_PORT:-8444}"
TUNNEL_ORIGIN="${TUNNEL_ORIGIN:-https://localhost:${HTTPS_PORT}}"

CF_DIR="${HOME}/.cloudflared"
CF_BIN="${CLOUDFLARED:-${CF_DIR}/../.local/bin/cloudflared}"
[[ -x "${CF_BIN}" ]] || CF_BIN="$(command -v cloudflared 2>/dev/null || echo "${HOME}/.local/bin/cloudflared")"
CONFIG="${CF_DIR}/${TUNNEL_NAME}.yml"

MODE="setup"
case "${1:-}" in
    --run)    MODE="run" ;;
    --status) MODE="status" ;;
    "") ;;
    *) echo "usage: $0 [--run | --status]"; exit 1 ;;
esac

# ── Status ──────────────────────────────────────────────────────────────────
if [[ "${MODE}" == "status" ]]; then
    echo
    say "Tunnel ${TUNNEL_NAME} → ${TUNNEL_HOSTNAME}"
    [[ -x "${CF_BIN}" ]] && ok "cloudflared: $("${CF_BIN}" --version 2>&1 | head -1)" || bad "cloudflared not installed"
    [[ -r "${CF_DIR}/cert.pem" ]] && ok "authorised with Cloudflare" || bad "not authorised — run scripts/tunnel_setup.sh"
    [[ -r "${CONFIG}" ]] && ok "config: ${CONFIG}" || bad "no config for ${TUNNEL_NAME}"
    if pgrep -f "cloudflared.*${TUNNEL_NAME}" >/dev/null 2>&1; then
        ok "running (pid $(pgrep -f "cloudflared.*${TUNNEL_NAME}" | head -1))"
    else
        warn "not running — make tunnel-run"
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${TUNNEL_HOSTNAME}/health" 2>/dev/null)"
    [[ "${code}" == "200" ]] && ok "https://${TUNNEL_HOSTNAME}/health answers 200" \
                             || bad "https://${TUNNEL_HOSTNAME}/health → ${code:-no answer}"
    echo
    exit 0
fi

echo
say "Public hostname for this machine"
note "tunnel ${TUNNEL_NAME} · hostname ${TUNNEL_HOSTNAME} · origin ${TUNNEL_ORIGIN}"
echo

# ── 1. The binary ───────────────────────────────────────────────────────────
if [[ -x "${CF_BIN}" ]]; then
    ok "cloudflared present: $("${CF_BIN}" --version 2>&1 | head -1)"
else
    say "Fetching cloudflared"
    note "Official static binary, into ~/.local/bin. No root, nothing system-wide."
    mkdir -p "${HOME}/.local/bin"
    CF_BIN="${HOME}/.local/bin/cloudflared"
    arch="$(uname -m)"
    case "${arch}" in
        x86_64) asset="cloudflared-linux-amd64" ;;
        aarch64|arm64) asset="cloudflared-linux-arm64" ;;
        *) bad "Unsupported architecture ${arch}. Install cloudflared yourself and re-run."; exit 1 ;;
    esac
    if ! curl -fsSL -o "${CF_BIN}" \
            "https://github.com/cloudflare/cloudflared/releases/latest/download/${asset}"; then
        bad "Download failed. Check the network, or install cloudflared by hand."
        exit 1
    fi
    chmod +x "${CF_BIN}"
    ok "installed: $("${CF_BIN}" --version 2>&1 | head -1)"
fi

# ── 2. Authorisation ────────────────────────────────────────────────────────
# cert.pem is scoped to one Cloudflare zone. A certificate for a *different*
# zone authenticates fine and then fails at the DNS step with a bare
# "Authentication error", which is why the zone is verified below rather than
# assumed from the file's presence.
if [[ -r "${CF_DIR}/cert.pem" ]]; then
    zone="$(python3 - "${CF_DIR}/cert.pem" <<'PY' 2>/dev/null
import base64, json, sys, urllib.request
raw = open(sys.argv[1]).read()
body = "".join(l for l in raw.splitlines() if "BEGIN" not in l and "END" not in l)
d = json.loads(base64.b64decode(body))
req = urllib.request.Request(
    f"https://api.cloudflare.com/client/v4/zones/{d['zoneID']}",
    headers={"Authorization": f"Bearer {d['apiToken']}"})
print(json.load(urllib.request.urlopen(req, timeout=20))["result"]["name"])
PY
)"
    if [[ -n "${zone}" && "${TUNNEL_HOSTNAME}" == *"${zone}" ]]; then
        ok "authorised for ${zone}"
    else
        bad "The stored authorisation is for ${zone:-an unknown zone}, not ${TUNNEL_HOSTNAME}."
        echo
        note "Move it aside and authorise the right zone:"
        note "    mv ~/.cloudflared/cert.pem ~/.cloudflared/cert.pem.old"
        note "    scripts/tunnel_setup.sh"
        exit 1
    fi
else
    say "Authorising with Cloudflare"
    note "A browser opens. Pick the zone that owns ${TUNNEL_HOSTNAME} and Authorize."
    note "This is the only step that needs you."
    echo
    "${CF_BIN}" tunnel login || { bad "Authorisation failed."; exit 1; }
    [[ -r "${CF_DIR}/cert.pem" ]] || { bad "No certificate was written."; exit 1; }
    ok "authorised"
fi

# ── 3. The tunnel ───────────────────────────────────────────────────────────
TUNNEL_ID="$("${CF_BIN}" tunnel list --output json 2>/dev/null \
    | python3 -c "
import json,sys
name = sys.argv[1]
try: rows = json.load(sys.stdin)
except Exception: rows = []
print(next((t['id'] for t in rows if t.get('name') == name), ''))
" "${TUNNEL_NAME}" 2>/dev/null)"

if [[ -n "${TUNNEL_ID}" ]]; then
    ok "tunnel ${TUNNEL_NAME} exists (${TUNNEL_ID:0:8}…)"
else
    say "Creating tunnel ${TUNNEL_NAME}"
    "${CF_BIN}" tunnel create "${TUNNEL_NAME}" >/dev/null 2>&1
    TUNNEL_ID="$("${CF_BIN}" tunnel list --output json 2>/dev/null \
        | python3 -c "
import json,sys
try: rows = json.load(sys.stdin)
except Exception: rows = []
print(next((t['id'] for t in rows if t.get('name') == sys.argv[1]), ''))
" "${TUNNEL_NAME}")"
    [[ -n "${TUNNEL_ID}" ]] || { bad "Could not create the tunnel."; exit 1; }
    ok "created ${TUNNEL_ID:0:8}…"
fi

CREDS="${CF_DIR}/${TUNNEL_ID}.json"
if [[ ! -r "${CREDS}" ]]; then
    bad "The tunnel exists on Cloudflare but this machine holds no credentials for it."
    echo
    note "Credentials are per-machine. Either copy ${TUNNEL_ID}.json from the"
    note "machine that created it into ~/.cloudflared/, or use a separate tunnel:"
    note "    TUNNEL_NAME=${TUNNEL_NAME}-$(hostname -s) scripts/tunnel_setup.sh"
    note "The hostname can be re-pointed freely; only one tunnel serves it at a time."
    exit 1
fi

# ── 4. The config ───────────────────────────────────────────────────────────
# Written per tunnel rather than to config.yml, which cloudflared reads by
# default: a machine may already run another tunnel from that file, and
# overwriting it would silently retire whatever it was serving.
umask 077
cat > "${CONFIG}" <<YAML
# Generated by scripts/tunnel_setup.sh. Safe to regenerate.
#
# The origin is Nginx, which already serves the pages and relays the API on a
# single TLS origin. noTLSVerify covers the loopback hop only: Nginx's own
# certificate names the public hostname, not localhost, and the public leg is
# terminated by Cloudflare with its certificate.
tunnel: ${TUNNEL_ID}
credentials-file: ${CREDS}
protocol: http2

ingress:
  - hostname: ${TUNNEL_HOSTNAME}
    service: ${TUNNEL_ORIGIN}
    originRequest:
      noTLSVerify: true
  - service: http_status:404
YAML
ok "config written: ${CONFIG}"

# ── 5. DNS ──────────────────────────────────────────────────────────────────
# `cloudflared tunnel route dns` is tried first, then the API directly. The
# subcommand refuses with a bare "Authentication error" in cases the API call
# handles fine, and it will not re-point a hostname that already resolves to a
# different tunnel — which is exactly what a second machine needs to do.
say "Pointing ${TUNNEL_HOSTNAME} at the tunnel"
if "${CF_BIN}" tunnel route dns "${TUNNEL_NAME}" "${TUNNEL_HOSTNAME}" >/dev/null 2>&1; then
    ok "DNS record created"
else
    result="$(python3 - "${CF_DIR}/cert.pem" "${TUNNEL_HOSTNAME}" "${TUNNEL_ID}" <<'PY'
import base64, json, sys, urllib.error, urllib.request

cert, hostname, tunnel_id = sys.argv[1], sys.argv[2], sys.argv[3]
raw = open(cert).read()
body = "".join(l for l in raw.splitlines() if "BEGIN" not in l and "END" not in l)
d = json.loads(base64.b64decode(body))
zone, token = d["zoneID"], d["apiToken"]
target = f"{tunnel_id}.cfargotunnel.com"


def api(method, path, payload=None):
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=json.dumps(payload).encode() if payload else None,
        method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as exc:
        return json.load(exc)


existing = (api("GET", f"/zones/{zone}/dns_records?name={hostname}").get("result") or [])
name = hostname.split(".")[0]
record = {"type": "CNAME", "name": name, "content": target, "proxied": True}
if existing:
    out = api("PATCH", f"/zones/{zone}/dns_records/{existing[0]['id']}", record)
    verb = "updated"
else:
    out = api("POST", f"/zones/{zone}/dns_records", record)
    verb = "created"
print(f"{verb}" if out.get("success") else f"failed: {out.get('errors')}")
PY
)"
    case "${result}" in
        created|updated) ok "DNS record ${result} → ${TUNNEL_ID:0:8}….cfargotunnel.com" ;;
        *) bad "Could not set DNS: ${result}"
           note "Add it by hand in the Cloudflare dashboard — DNS → Add record:"
           note "    CNAME  ${TUNNEL_HOSTNAME%%.*}  ${TUNNEL_ID}.cfargotunnel.com  (proxied)"
           exit 1 ;;
    esac
fi

echo
ok "Tunnel configured."
echo

if [[ "${MODE}" == "run" ]]; then
    say "Running (Ctrl-C stops it, and the hostname stops answering)"
    exec "${CF_BIN}" tunnel --config "${CONFIG}" run "${TUNNEL_NAME}"
fi

echo "  Run it:      ${BOLD}make tunnel-run${OFF}"
echo "  In the background:"
echo "    ${DIM}setsid ${CF_BIN} tunnel --config ${CONFIG} run ${TUNNEL_NAME} </dev/null >/tmp/tunnel.log 2>&1 &${OFF}"
echo
echo "  Then:        ${BOLD}https://${TUNNEL_HOSTNAME}${OFF}"
echo
