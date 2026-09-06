# GK-040: encrypted PostgreSQL backups with pluggable object storage.
#
# Pinned to postgres-client 16 to match the prod database image. `age` gives
# us a single-keypair encryption story without needing GPG infrastructure;
# `rclone` lets the same image push to S3, R2, B2, or any S3-compatible
# bucket by swapping config — no code changes per provider.
FROM alpine:3.19

RUN apk add --no-cache \
    postgresql16-client=~16 \
    age \
    rclone \
    coreutils \
    tzdata \
    ca-certificates \
    busybox-suid \
    bash \
    curl

# Cron-friendly defaults; the entrypoint script reads them at run time.
ENV BACKUP_RETENTION_DAYS=14 \
    BACKUP_DIR=/var/backups/membership_saas \
    BACKUP_INTERVAL_SECONDS=86400 \
    TZ=UTC

WORKDIR /opt/membership_saas-backup

COPY backup.sh ./backup.sh
COPY restore.sh ./restore.sh
COPY entrypoint.sh ./entrypoint.sh
# GK-437: the restore canary ships in the same image but runs as its own
# service with its own entrypoint — see docker-compose.yml. Same tools, and
# only that service gets the age private key mounted.
COPY verify.sh ./verify.sh
COPY verify-entrypoint.sh ./verify-entrypoint.sh
# GK-427: does a given copy of the private key actually open our backups? Ships
# in the image so the check needs no local `age` install — on the host, or in
# the client's hands at handoff (GK-441).
COPY check-identity.sh ./check-identity.sh

RUN chmod +x ./backup.sh ./restore.sh ./entrypoint.sh ./verify.sh ./verify-entrypoint.sh ./check-identity.sh \
    && mkdir -p "$BACKUP_DIR"

# Run as a non-root user so a mis-mounted host volume cannot be chowned
# accidentally. UID 1001 matches the backend container for shared volumes.
RUN addgroup -g 1001 backup && adduser -D -u 1001 -G backup backup \
    && chown -R backup:backup /opt/membership_saas-backup "$BACKUP_DIR"

USER backup:backup
ENTRYPOINT ["./entrypoint.sh"]
