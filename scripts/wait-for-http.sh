#!/usr/bin/env bash
#
# Poll a URL until it answers with an expected HTTP status.
#
#   wait-for-http.sh URL EXPECT ATTEMPTS BODY_FILE
#
# EXPECT is either a literal three-digit status ("503") or "2xx" for any success
# code. BODY_FILE receives the last response body. Exits 0 on the first match,
# 1 if ATTEMPTS one-second polls go by without one.
#
# Why this exists rather than an inline `for` loop in the workflow: the obvious
# spelling of that loop is wrong in a way that is easy to miss. GitHub Actions
# runs `run:` blocks under `bash -e`, and
#
#     status="$(curl -s -w '%{http_code}' "${url}")"
#
# is a plain assignment, so a curl that fails at the network level — connection
# refused or reset, which is precisely what happens while a server is starting
# up or reacting to its database disappearing — makes the assignment non-zero
# and `set -e` kills the step. A retry loop written that way cannot survive the
# transient it exists to retry through; it dies on the first one and reports a
# bare "exit code 7". Hence `|| true` and the explicit 000 below.
set -euo pipefail

url="${1:?usage: wait-for-http.sh URL EXPECT ATTEMPTS BODY_FILE}"
expect="${2:?missing EXPECT (a status code, or 2xx)}"
attempts="${3:?missing ATTEMPTS}"
body_file="${4:?missing BODY_FILE}"

matches() {
    case "${expect}" in
        2xx) [[ "$1" == 2?? ]] ;;
        *) [ "$1" = "${expect}" ] ;;
    esac
}

status=000
for attempt in $(seq 1 "${attempts}"); do
    # `|| true` keeps a network-level curl failure from tripping `set -e`; the
    # 000 fallback covers the case where curl writes nothing to stdout.
    status="$(curl -s -o "${body_file}" -w '%{http_code}' "${url}" || true)"
    status="${status:-000}"

    if matches "${status}"; then
        echo "${url} returned ${status} after ${attempt}s"
        exit 0
    fi

    echo "waiting for ${url} to return ${expect} (${attempt}/${attempts}, got ${status})..."
    sleep 1
done

echo "::error::${url} never returned ${expect}; last status was ${status} after ${attempts}s"
exit 1
