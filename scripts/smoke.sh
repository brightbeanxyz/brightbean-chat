#!/usr/bin/env bash
#
# Prove a deployment is healthy AND hardened.
#
#   scripts/smoke.sh https://chat.example.com
#   scripts/smoke.sh https://localhost --insecure --project bbchat-prod
#
# Options:
#   --insecure            accept a self-signed / internal-CA certificate. Caddy
#                         issues one of those whenever APP_DOMAIN is `localhost`,
#                         which is how this stack is tested locally and in CI.
#   --http-url URL        the plain-HTTP origin, for the redirect check. Derived
#                         from the base URL when omitted.
#   --tick-token TOKEN    also prove /internal/tick accepts the real token.
#   --verify-token TOKEN  also prove Meta's verification GET echoes a challenge
#                         (this is PLATFORM_INSTAGRAM_VERIFY_TOKEN).
#   --db-host HOST        prove Postgres is NOT reachable there on 5432.
#   --project NAME        prove the compose project's app container is non-root.
#   --compose-file PATH   which compose file --project refers to.
#
# Checks the four security headers SECURITY-BASELINE §8 requires at the proxy,
# so it is equally valid against Caddy, a PaaS router or your own nginx —
# whatever is actually in front is what answers.
#
# `|| true` on every curl, and an explicit 000 fallback, for the reason
# scripts/wait-for-http.sh spells out at length: a plain assignment from a curl
# that fails at the network level is non-zero, and under `set -e` that kills the
# script instead of letting it report which check failed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

base_url=""
http_url=""
tick_token=""
verify_token=""
db_host=""
project=""
compose_file="${REPO_ROOT}/docker-compose.prod.yml"
curl_opts=(--silent --show-error)
wait_curl_opts=""

usage() {
    sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --insecure) curl_opts+=(--insecure); wait_curl_opts="--insecure"; shift ;;
        --http-url) http_url="${2:?--http-url needs a URL}"; shift 2 ;;
        --tick-token) tick_token="${2:?--tick-token needs a value}"; shift 2 ;;
        --verify-token) verify_token="${2:?--verify-token needs a value}"; shift 2 ;;
        --db-host) db_host="${2:?--db-host needs a host}"; shift 2 ;;
        --project) project="${2:?--project needs a name}"; shift 2 ;;
        --compose-file) compose_file="${2:?--compose-file needs a path}"; shift 2 ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $1" >&2; usage ;;
        *) [ -z "${base_url}" ] || { echo "unexpected argument: $1" >&2; usage; }
           base_url="${1%/}"; shift ;;
    esac
done

[ -n "${base_url}" ] || usage

# The plain-HTTP origin, for the redirect check: same host, no scheme, and
# without an explicit :443 which would not be listening for HTTP.
if [ -z "${http_url}" ]; then
    http_url="http://${base_url#*://}"
    http_url="${http_url%:443}"
fi

failures=0
checks=0

pass() { checks=$((checks + 1)); printf '  ok    %s\n' "$1"; }
fail() { checks=$((checks + 1)); failures=$((failures + 1)); printf '  FAIL  %s\n' "$1" >&2; }
section() { printf '\n%s\n' "$1"; }

# Status of a URL, without following redirects. 000 means the request never
# completed — refused, reset, or a TLS failure.
status_of() {
    local url="$1" out="${2:-/dev/null}"
    local code
    code="$(curl "${curl_opts[@]}" -o "${out}" -w '%{http_code}' "${url}" 2>/dev/null || true)"
    printf '%s' "${code:-000}"
}

expect_status() {
    local url="$1" expected="$2" label="$3" out="${4:-/dev/null}"
    local got
    got="$(status_of "${url}" "${out}")"
    if [ "${got}" = "${expected}" ]; then
        pass "${label} (${got})"
    else
        fail "${label}: expected ${expected}, got ${got}"
    fi
}

body_file="$(mktemp)"
header_file="$(mktemp)"
trap 'rm -f "${body_file}" "${header_file}"' EXIT

# ---------------------------------------------------------------------------
section "Health (SPEC §20)"
# ---------------------------------------------------------------------------

# Wait rather than assert immediately: this script is run right after `up -d`
# as often as against a deployment that has been serving for a month.
export WAIT_FOR_HTTP_CURL_OPTS="${wait_curl_opts}"
if ! "${REPO_ROOT}/scripts/wait-for-http.sh" \
        "${base_url}/healthz" 2xx 60 "${body_file}" >/dev/null 2>&1; then
    # Re-run visibly so the operator sees the last status rather than silence.
    "${REPO_ROOT}/scripts/wait-for-http.sh" "${base_url}/healthz" 2xx 1 "${body_file}" || true
fi

if grep -q '"status": *"ok"' "${body_file}" && grep -q '"database": *"ok"' "${body_file}"; then
    pass "/healthz reports the database round-trip succeeded"
else
    fail "/healthz did not report status=ok and database=ok: $(head -c 200 "${body_file}")"
fi

# ---------------------------------------------------------------------------
section "TLS (SECURITY-BASELINE §8)"
# ---------------------------------------------------------------------------

http_root_status="$(status_of "${http_url}/")"
case "${http_root_status}" in
    301|302|307|308) pass "plain HTTP is redirected to HTTPS (${http_root_status})" ;;
    000) fail "plain HTTP origin ${http_url}/ is not reachable, so the redirect could not be checked" ;;
    *) fail "plain HTTP served ${http_root_status} instead of redirecting to HTTPS" ;;
esac

# config/settings/production.py exempts /healthz from Django's SSL redirect so
# in-network probes reaching the app directly are not answered with a 301. That
# exemption is invisible from out here: Caddy redirects plain HTTP at the edge,
# before the request ever reaches Django. So a redirect is the RIGHT answer
# through the proxy, and 200 is the right answer when something forwards plain
# HTTP straight to the app. Anything else — a 400, a 502, a 404 — means the probe
# path is broken, which is exactly the failure that leaves a container marked
# unhealthy with nothing in the logs to say why.
http_healthz_status="$(status_of "${http_url}/healthz")"
case "${http_healthz_status}" in
    200) pass "/healthz answers 200 over plain HTTP (nothing redirecting in front)" ;;
    301|302|307|308) pass "/healthz is redirected to HTTPS at the edge (${http_healthz_status})" ;;
    000) pass "/healthz over plain HTTP is unreachable (TLS terminated elsewhere)" ;;
    *) fail "/healthz over plain HTTP returned ${http_healthz_status}: expected 200 or a redirect" ;;
esac

# ---------------------------------------------------------------------------
section "Security headers (SECURITY-BASELINE §8)"
# ---------------------------------------------------------------------------

# Resolve the redirect chain first, then fetch the final URL on its own, so the
# headers examined belong to one response rather than to whichever hop curl
# printed last.
final_url="$(curl "${curl_opts[@]}" -L -o /dev/null -w '%{url_effective}' "${base_url}/" 2>/dev/null || true)"
final_url="${final_url:-${base_url}/}"
curl "${curl_opts[@]}" -D "${header_file}" -o "${body_file}" "${final_url}" >/dev/null 2>&1 || true

header_value() {
    # Header names are case-insensitive; values may carry a trailing CR.
    awk -v want="$(printf '%s' "$1" | tr 'A-Z' 'a-z')" '
        BEGIN { FS=": " }
        { name = $1; sub(/:.*/, "", name); if (tolower(name) == want) { sub(/^[^:]*: */, ""); gsub(/\r/, ""); print } }
    ' "${header_file}" | tail -n 1
}

expect_header() {
    local name="$1" pattern="$2"
    local value
    value="$(header_value "${name}")"
    if [ -z "${value}" ]; then
        fail "${name} is missing"
    elif printf '%s' "${value}" | grep -qi -- "${pattern}"; then
        pass "${name}: ${value}"
    else
        fail "${name} is '${value}', expected to match '${pattern}'"
    fi
}

expect_header "Strict-Transport-Security" "max-age=[1-9][0-9]\{6,\}"
expect_header "Strict-Transport-Security" "includeSubDomains"
expect_header "X-Content-Type-Options" "nosniff"
expect_header "X-Frame-Options" "DENY"
expect_header "Referrer-Policy" "."

if grep -qi '^content-security-policy:' "${header_file}"; then
    pass "Content-Security-Policy is set (Django, with per-request nonces)"
else
    fail "Content-Security-Policy is missing"
fi

if grep -q "BrightBean Chat" "${body_file}"; then
    pass "the app shell renders at ${final_url}"
else
    fail "${final_url} did not render the app shell"
fi

# ---------------------------------------------------------------------------
section "Unauthenticated endpoints answer 404, not 403 (SECURITY-BASELINE §4)"
# ---------------------------------------------------------------------------

expect_status "${base_url}/internal/tick" "404" "/internal/tick with no token"
expect_status "${base_url}/internal/tick?token=definitely-not-the-token" "404" "/internal/tick with a wrong token"

if [ -n "${tick_token}" ]; then
    tick_status="$(status_of "${base_url}/internal/tick?token=${tick_token}" "${body_file}")"
    if [ "${tick_status}" = "200" ] && grep -q '"claimed"' "${body_file}"; then
        pass "/internal/tick drains the queue with the real token"
    else
        fail "/internal/tick with the real token returned ${tick_status}: $(head -c 200 "${body_file}")"
    fi
fi

# Meta's subscription check. A platform with no verify token configured must
# answer 404 — an endpoint that cannot verify anything should not advertise that
# it exists, and nothing can be subscribed to it by accident.
challenge_url="${base_url}/webhooks/instagram/?hub.mode=subscribe&hub.challenge=1234567&hub.verify_token="
if [ -n "${verify_token}" ]; then
    verify_status="$(status_of "${challenge_url}${verify_token}" "${body_file}")"
    if [ "${verify_status}" = "200" ] && grep -q '^1234567$' "${body_file}"; then
        pass "the Instagram webhook echoes Meta's challenge for the right verify token"
    else
        fail "the Instagram webhook verification returned ${verify_status}: $(head -c 200 "${body_file}")"
    fi
    expect_status "${challenge_url}wrong-token" "403" "webhook verification with a wrong token"
else
    expect_status "${challenge_url}anything" "404" \
        "the Instagram webhook refuses verification while no verify token is configured"
fi

# ---------------------------------------------------------------------------
if [ -n "${db_host}" ]; then
section "Postgres is not exposed (issue #28 acceptance)"

    probe_tcp() {
        local host="$1" port="$2"
        if command -v nc >/dev/null 2>&1; then
            nc -z -w 3 "${host}" "${port}" >/dev/null 2>&1
        elif command -v python3 >/dev/null 2>&1; then
            python3 - "${host}" "${port}" <<'PY'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
except OSError:
    sys.exit(1)
PY
        else
            return 2
        fi
    }

    set +e
    probe_tcp "${db_host}" 5432
    probe_result=$?
    set -e
    case "${probe_result}" in
        0) fail "Postgres answered on ${db_host}:5432 — it must not be published" ;;
        1) pass "Postgres is unreachable on ${db_host}:5432" ;;
        *) printf '  skip  no nc or python3 available to probe %s:5432\n' "${db_host}" ;;
    esac
fi

# ---------------------------------------------------------------------------
if [ -n "${project}" ]; then
section "Containers run as a non-root user (Layer-0 gate item 4)"

    uid="$(docker compose -p "${project}" -f "${compose_file}" exec -T app id -u 2>/dev/null | tr -d '\r' || true)"
    if [ -z "${uid}" ]; then
        fail "could not read the app container's uid in compose project '${project}'"
    elif [ "${uid}" = "0" ]; then
        fail "the app container runs as root"
    else
        pass "the app container runs as uid ${uid}"
    fi
fi

# ---------------------------------------------------------------------------
printf '\n'
if [ "${failures}" -eq 0 ]; then
    printf '%s checks passed against %s\n' "${checks}" "${base_url}"
    exit 0
fi
printf '%s of %s checks FAILED against %s\n' "${failures}" "${checks}" "${base_url}" >&2
exit 1
