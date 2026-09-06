#!/usr/bin/env bash
# GK-437: run verify.sh on an interval.
#
# Deliberately a separate container from the backup loop, not another job
# inside it. The backup container writes dumps and only ever needs the age
# PUBLIC key; the canary reads them and needs the PRIVATE one. Keeping them
# apart means the process that runs every night against a network-reachable
# database never holds the key that opens every dump we have.
#
# Unlike the backup entrypoint, this one does NOT alert from the shell. The
# verdict goes into `backup_verifications`, and the bot's daily
# `backup_verification_job` is what shouts — including about this container
# being dead, which a script inside it could never report.

set -uo pipefail

interval="${BACKUP_VERIFY_INTERVAL_SECONDS:-86400}"  # 24h default
# Wait before the first run so a fresh `up -d` does not race the backup
# container's own start-up dump, and so a deploy that restarts everything at
# once does not immediately verify a dump that is being written right now.
initial_delay="${BACKUP_VERIFY_INITIAL_DELAY_SECONDS:-300}"

log() { printf '[verify-entrypoint] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

run_once() {
    # `./verify.sh && return 0` rather than an `if`: after a false `if` with no
    # `else`, bash resets $? to 0 and the log line always claims exit 0. Same
    # trap the backup entrypoint fell into.
    ./verify.sh && return 0
    rc=$?
    log "verify.sh failed (exit $rc) — the verdict is in backup_verifications; the bot alerts on it"
    return 1
}

log "starting: first check in ${initial_delay}s, then every ${interval}s"
sleep "$initial_delay"
run_once || true

while true; do
    sleep "$interval"
    run_once || true
done
