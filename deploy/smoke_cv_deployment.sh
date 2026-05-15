#!/usr/bin/env bash
# Smoke test for surgical-cv FastAPI deployment on localhost.
# Confirms uvicorn is serving and both Gradio mounts respond.
#
# Run after `sudo systemctl start surgical-cv-app` and before exposing via
# Cloudflare Tunnel.
#
# Exit code: 0 if all pass, 1 if any fail.

set -u

PORT="${SURGICAL_CV_PORT:-7865}"
BASE="http://127.0.0.1:${PORT}"
PASS=0
FAIL=0

check() {
  local description=$1
  local expected_codes=$2
  local url=$3
  local actual
  actual=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")
  if [[ " $expected_codes " == *" $actual "* ]]; then
    printf "PASS  %-40s (%s)\n" "$description" "$actual"
    PASS=$((PASS+1))
  else
    printf "FAIL  %-40s (got %s, expected: %s)\n" "$description" "$actual" "$expected_codes"
    FAIL=$((FAIL+1))
  fi
}

echo "=== surgical-cv localhost smoke ==="
echo "Target: $BASE"
echo
check "healthz responds"            "200"               "$BASE/healthz"
check "surgeon mount reachable"     "200 302 303 401"   "$BASE/app/"
check "admin mount reachable"       "200 302 303 401"   "$BASE/admin/"
echo
echo "Passed: $PASS / $((PASS+FAIL))"

if [[ $FAIL -gt 0 ]]; then
  echo "FAILED — check journal: sudo journalctl -u surgical-cv-app -n 50"
  exit 1
fi

echo "All checks passed. Ready for Cloudflare ingress setup."
exit 0
