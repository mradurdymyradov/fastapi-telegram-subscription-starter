# Backup and Restore Runbook (GK-427)

How the production database is backed up, how to prove the backups are real,
and how to restore one. Companion to `ops_baseline.md` (which describes the
GK-040 machinery) and `deployment.md`.

**The rule this runbook exists to enforce: a backup nobody has restored is a
hypothesis.** Do not record backups as working on the strength of a dump
appearing in a bucket. Restore one and compare row counts.

> ## ⚠️ The off-host target is INTERIM and must move at handoff
>
> Since 2026-08-10 backups go to **the maintainer's personal Google Drive**
> (`gdrive:membership_saas-backups/prod`), not to client-owned storage.
>
> This is not the design. The intended target was the Cloudflare R2 bucket on
> the **client's** account. That credential was revoked at Cloudflare's end
> before 2026-08-09 and only the client can reissue it. Rather than leave the
> only copies of the database on the machine that holds the database, the
> target moved to storage we control.
>
> The payload is `age`-encrypted on the host before upload, so Google holds
> only ciphertext and cannot read member data. That limits the exposure; it
> does not make the arrangement correct. **At handoff this must be repointed
> to client-owned storage — tracked as GK-441.** Repointing is a config
> change, not a code change: a new block in `rclone.conf` and a new
> `BACKUP_RCLONE_REMOTE`.

---

## What runs

The `backup` service in `deploy/docker-compose.yml`, behind the `backup`
compose profile so it never starts by accident in local development.

```
entrypoint.sh   loop: run backup.sh, sleep BACKUP_INTERVAL_SECONDS, repeat
                (runs once immediately on container start)
backup.sh       pg_dump | gzip -9  →  age --recipient <pubkey>  →  rclone copy
                then prunes LOCAL copies older than BACKUP_RETENTION_DAYS
restore.sh      rclone copy ← remote, age --decrypt, psql
```

Dumps are `--no-owner --no-privileges --no-acl`, so they restore onto a host
with different role names. Encryption is `age` with a single recipient
keypair: only the **public** key is on the server, so a compromised VPS cannot
decrypt its own backups.

### Settings (live `.env`)

| Variable | Live value | Meaning |
|---|---|---|
| `BACKUP_AGE_PUBLIC_KEY` | `age1y46nm…us90h8` | encryption recipient |
| `BACKUP_RCLONE_REMOTE` | `gdrive:membership_saas-backups/prod` | Google Drive target (interim — see the box at the top) |
| `BACKUP_RETENTION_DAYS` | `14` | **local** pruning only |
| `BACKUP_REMOTE_RETENTION_DAYS` | `30` | prunes the **remote**. Leave unset on S3/R2/B2 — see *Remote retention* |
| `BACKUP_INTERVAL_SECONDS` | `86400` | once a day |
| `BACKUP_RCLONE_CONF` | `/opt/membership_saas/secrets/rclone.conf` | credentials, outside the release tree |
| `BACKUP_ALERT_WEBHOOK_URL` | *(empty — not needed)* | optional extra webhook |

**The recipient key changed on 2026-08-09.** Earlier revisions of this runbook
documented `age1t6twp5z…9st2rk`; the live recipient is `age1y46nm…us90h8`.
Both identities exist in Bitwarden and **both matter** — a dump can only be
opened by the identity it was encrypted to, so the superseded key is exactly
as sensitive as the current one and must not be discarded. Always confirm
with `age-keygen -y <identity>` that you hold the right one before concluding
a backup is corrupt.

Failure alerting does **not** depend on that last variable: `entrypoint.sh`
posts to the same Telegram ops chat as the rest of the system, using
`BOT_TOKEN` + `ALERT_CHAT_ID` from `.env`.

---

## ⚠️ Three traps that made this silently not work

**1. `rclone.conf` must live outside `releases/`.** It holds the R2 keys, so it
is gitignored and never travels in the release. `deploy.yml` rsyncs the
checkout with `--delete` and carries forward **only** `.env` — so a config
placed at `current/deploy/backup/rclone.conf` is destroyed by the next deploy,
`rclone copy` starts failing, and nothing else changes visibly. That is why the
compose mount reads `${BACKUP_RCLONE_CONF:-./backup/rclone.conf}` and the live
host sets it to `/opt/membership_saas/secrets/rclone.conf`.

The 2026-08-07 verification pass recorded this task as "the machinery is built
and configured, nobody started the service". That was half right: all four
`BACKUP_*` settings were indeed present in `.env`, but `rclone.conf` was
**absent from the server entirely**, so starting the service as it stood would
have produced a dump, encrypted it, and then failed at the upload step.

**2. Losing the age private key loses every backup.** The server holds only the
public key — by design. The private key lives at `deploy/backup/age-identity.txt`
on the maintainer's machine and is gitignored. **It must also exist in at least
one place that is not that machine** — a password manager entry, or the client's
vault at handoff. Without it the encrypted dumps are noise.

**There are two keys in this project's history, and only one of them works.**
Identify any copy by its public half before trusting it:

| Public key | Created | Status |
|---|---|---|
| `age1y46nmqngus75xuexxrwcjgdqdu0y7ce9c5fursfxjm08xfa5eynsus90h8` | 2026-08-09T17:47:39Z | **live** — every dump from 2026-08-09 17:39 UTC onward is encrypted to this |
| `age1t6twp5z9ucmhz552jdgfvl8acslpa5vywtdukq92k8aeuf3e74aq9st2rk` | — | **dead** — the private half fails its own checksum (`malformed secret key: invalid checksum`, re-measured 2026-08-18). Kept only as `age-identity.txt.corrupt-20260809.bak`. Every dump predating the rotation is permanently unreadable |

Check any copy with `check-identity.sh`, which derives the public half and
compares it to the live recipient for you. Use it on **any** copy whose
provenance you are not certain of — the password-manager entry, the one handed
to the client at transfer, the one found on an old laptop:

```bash
docker run --rm --entrypoint ./check-identity.sh \
  -v /path/to/age-identity.txt:/k/id.txt:ro \
  membership_saas-backup:latest /k/id.txt
```

`PASS` means that copy opens our backups. It distinguishes the three failures
that matter: a key that fails its own checksum (the 09.08 mode — looks like a
key, is not one), a valid key that is the **wrong** key, and a file that is not
an identity at all. The raw equivalent, if you have `age` locally:

```bash
age-keygen -y /path/to/age-identity.txt   # must print age1y46nmqng…
```

> Do not trust the `# public key:` comment inside the file. The 09.08 key
> carried a perfectly well-formed header above a secret that does not parse.
> Derivation is the only thing that proves anything.

> ⚠️ **The off-machine copy is not verified.** An earlier note here read
> "Confirmed 2026-08-10: the identity is in Bitwarden" — but the fingerprint it
> named alongside that claim was the **dead** key's. The Bitwarden entry may well
> hold the correct key and only this document was stale; the point is that
> nobody has run `age-keygen -y` against the stored copy and compared the output
> to the live fingerprint above. Until somebody does, the private key effectively
> exists once, on the maintainer's PC, and every encrypted dump on the VPS and in
> Drive dies with that machine. **This is the open half of GK-427.**

It becomes proven the first time someone other than the maintainer restores using
only the stored copy. Custody transfers to the client at handoff (GK-441 / GK-150).

**3. A revoked storage credential looks exactly like a working one.** The R2
token died at Cloudflare's end with nothing on our side changing: the stored
key stayed byte-identical to the copy that worked in June, and every nightly
cycle kept dumping and encrypting perfectly before failing on the last step.
Two lessons are now baked into the setup:

- **Never leave a dead credential block in a config.** The `[r2]` block was
  *deleted* from the live `rclone.conf`, not commented out. A revoked
  credential sitting next to a live one is something a future reader will
  eventually mistake for the real thing.
- **Check whose account it is before debugging the config.** For S3-style
  remotes the endpoint host *is* the account id
  (`https://<account-id>.r2.cloudflarestorage.com`). The R2 account
  (`b24dd3e3…`) turned out to be the **client's**, not the maintainer's
  (`fe561432…`) — so no amount of local debugging could ever have fixed it.
  Establish ownership first; it decides who can even act.

---

## Starting / checking the service

```bash
cd /opt/membership_saas/current/deploy
docker compose -p membership_saas --profile backup up -d --build backup
docker logs --tail 50 membership_saas-backup-1
```

A healthy first cycle logs, in order:

```
[backup] … starting dump host=db db=membership_saas target=gdrive:membership_saas-backups/prod
[backup] … dump size <N> bytes — encrypting
[backup] … uploading membership_saas-<stamp>.sql.gz.age → gdrive:…
[backup] … ok ts=<stamp> size=<N> remote=gdrive:…
```

Confirm the object actually landed off-host — the log line above is written
before nothing has been verified remotely:

```bash
docker exec membership_saas-backup-1 rclone lsl gdrive:membership_saas-backups/prod
```

## Restoring — the rehearsal, and the real thing

Restore into a **scratch database on the same host**, never over the live one,
unless you are genuinely recovering. `restore.sh` refuses to run without
`RESTORE_CONFIRM=YES`.

There are two ways to do this and they trade off differently. Know which one
you are choosing:

- **On the host** (below). Convenient, and the only practical option for the
  nightly canary. It requires the age **private** key to be on the server,
  even if only briefly — so for that window the machine holding the database
  also holds the means to read every off-host backup of it.
- **On an operator machine.** Stream the object down and never let the key
  near the server. Slower over a thin link, but it keeps the separation the
  encrypt-only design exists to create. This is how the 2026-08-10 proof was
  done:

  ```bash
  ssh root@127.0.0.1 \
    'docker run --rm -v /opt/membership_saas/secrets/rclone.conf:/cfg.conf:ro \
       --entrypoint rclone membership_saas-backup --config /cfg.conf \
       cat gdrive:membership_saas-backups/prod/membership_saas-<stamp>.sql.gz.age' \
    > from-drive.sql.gz.age
  age --decrypt -i age-identity.txt -o dump.sql.gz from-drive.sql.gz.age
  ```

  Delete the plaintext and the downloaded object afterwards — they hold real
  member data.

The canary (below) accepts the first trade-off deliberately and mitigates it
by running as a **separate container** from the backup writer. That is a
considered decision, not an oversight — but it is the reason the private key
now sits in `/opt/membership_saas/secrets/`, and anyone reasoning about the
blast radius of a host compromise needs to know it is there.

```bash
# 1. scratch database
docker exec membership_saas-db-1 psql -U membership_saas -d postgres \
  -c 'DROP DATABASE IF EXISTS restore_check;' -c 'CREATE DATABASE restore_check;'

# 2. newest encrypted object
docker exec membership_saas-backup-1 rclone lsf gdrive:membership_saas-backups/prod | sort | tail -1

# 3. private key into the container (from the maintainer's machine), restore,
#    then remove it again — do not leave the identity on the server
scp -i ~/.ssh/gkclub_hetzner_ed25519 deploy/backup/age-identity.txt \
    root@127.0.0.1:/tmp/age-identity.txt
ssh -i ~/.ssh/gkclub_hetzner_ed25519 root@127.0.0.1 \
  "docker cp /tmp/age-identity.txt membership_saas-backup-1:/tmp/id.txt && rm -f /tmp/age-identity.txt"

docker exec \
  -e RESTORE_CONFIRM=YES \
  -e BACKUP_AGE_IDENTITY_FILE=/tmp/id.txt \
  -e POSTGRES_DB=restore_check \
  membership_saas-backup-1 ./restore.sh <object-name>

docker exec membership_saas-backup-1 rm -f /tmp/id.txt

# 4. compare against live — this is the step that makes it evidence
```

Row-count comparison (the check that matters):

```bash
docker exec membership_saas-db-1 bash -lc '
for db in membership_saas restore_check; do
  echo "== $db";
  psql -U membership_saas -d $db -At -c "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema=\"public\" ORDER BY 1" |
  while read t; do
    printf "%s %s\n" "$t" "$(psql -U membership_saas -d $db -At -c "SELECT count(*) FROM \"$t\"")";
  done;
done'
```

Drop the scratch database afterwards:

```bash
docker exec membership_saas-db-1 psql -U membership_saas -d postgres \
  -c 'DROP DATABASE restore_check;'
```

## Proving the failure alert

A backup that breaks quietly is the worst case, so the alert path is worth
exercising once deliberately:

```bash
docker exec -e BACKUP_RCLONE_REMOTE=r2:this-bucket-does-not-exist/prod \
  membership_saas-backup-1 ./backup.sh   # expect a non-zero exit
```

That exercises `backup.sh`'s failure only. To see the alert itself, point the
running container at a bad remote and let `entrypoint.sh` run a cycle — the
message lands in the ops chat "Alerts - Owner Community Bot"
(`-1003722846405`) as `🚨 membership_saas ops / DATABASE BACKUP FAILED`.

The alert has two distinct shapes, and they mean different things:

- `🚨 DATABASE BACKUP FAILED` — exit 1. No new backup exists. The newest good
  dump is whatever preceded this run.
- `⚠️ Backup uploaded OK — but old copies are NOT being deleted` — exit 3.
  Today's backup is safe; remote retention did not run. Not a data-loss event,
  but it becomes one if ignored long enough for storage to fill.

---

## Reissuing the Google Drive token

The stored `token` is an OAuth blob containing a refresh token. It survives
restarts and deploys, but dies if the account owner revokes rclone's access or
Google expires it. Symptom is `couldn't fetch token` / `invalid_grant` on
upload.

You cannot hand-write a replacement — it must be generated through Google's
consent screen. The server has no browser, so forward the callback:

**1. Start the authorization on the server** (rclone lives in the backup
image, not on the host):

```bash
ssh root@127.0.0.1 \
  'docker run --rm --network host --name gk427-auth --entrypoint rclone \
     membership_saas-backup authorize "drive" --drive-scope drive.file \
     --auth-no-open-browser'
```

`--network host` matters: rclone binds its callback to `127.0.0.1:53682`,
which inside a normal container is unreachable from a published port. It
prints a `http://127.0.0.1:53682/auth?state=…` link and waits.

**2. Forward the port from a machine with a browser.** Use keepalives and a
reconnect loop — on a thin link the tunnel drops silently and the callback
lands nowhere:

```bash
while true; do ssh -N -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 53682:127.0.0.1:53682 root@127.0.0.1; sleep 3; done
```

**3. Open the printed link locally**, sign in, click through "Google hasn't
verified this app" (*Advanced → Go to rclone*), Allow.

**4. Write the blob into `/opt/membership_saas/secrets/rclone.conf`** under
`[gdrive]` as `token = {…}`, `chmod 400`, owned by uid 1001. **Shred the
authorize output** — that log holds a live credential. Restart and re-run the
proof.

Keep `scope = drive.file`: rclone then sees only files it created itself,
never the rest of that Google account. A side effect worth knowing — `rclone
lsd gdrive:` legitimately returns nothing. List the prefix, not the root.

**If you see `Failed to save config … permission denied`:** that is rclone
trying to persist a refreshed access token into the read-only config mount.
`backup.sh` works around it by handing rclone a throwaway writable copy, so it
should not appear there — but it *will* appear on any ad-hoc `docker run …
rclone` you type by hand against a `:ro` config. Harmless in that context.
Do not "fix" it by making the real credential file writable.

### Reissuing the R2 token (historical — for the handoff back)

The endpoint host *is* the account id, so check ownership first. Cloudflare
dashboard → **R2** → **API** → **Manage API tokens** → *Create API token*,
type **R2 token**, permission **Object Read & Write**, scoped to **the one
bucket** — never account-wide. Copy Access Key ID, Secret Access Key and
endpoint into `rclone.conf`, restart, run the proof, then **delete the old
token** so a rejected credential cannot later be mistaken for the live one.

---

## The restore canary (GK-437)

Everything above is a rehearsal a human performs. The canary is the same thing
on a schedule, so the claim stops being "we restored one once in August".

**What it is.** A `backup-verify` service, same image as `backup`, different
entrypoint. Nightly it takes the newest encrypted dump, decrypts it, restores it
into a scratch database, compares every table against live, drops the scratch
database, and writes one row into `backup_verifications`. Failures are rows too.

**Why it is a separate container.** It needs the age **private** key; the
backup writer needs only the public one. Keeping them apart means the process
running every night against a network-reachable database is not the one holding
the key that opens every dump we have.

**What it does not do — and what covers that.** A dead canary writes nothing,
and "nothing" looks exactly like a healthy quiet system. So the bot runs
`backup_verification_job` daily at 06:30 UTC and alerts on three distinct
states: `never` (no verification has ever run), `stale` (nothing since
`BACKUP_VERIFICATION_MAX_AGE_HOURS`, default 36), and `failed`. The admin
dashboard shows the same thing, including the quiet success line — the point of
this task is that "the newest backup was restorable as of X" is readable.

### Turning it on

> **Done on 2026-08-18 — the canary is running on production.** This section is
> kept as the procedure for a rebuild or a new host, not as outstanding work.
> Current state: key installed at `/opt/membership_saas/secrets/age-identity.txt`
> (600, `1001:1001`), the five `BACKUP_VERIFY_*`/`BACKUP_AGE_IDENTITY_HOST_FILE`
> lines appended to the live `.env`, `backup-verify` up with `restart:
> unless-stopped`. First verdict: `ok=t`, 30 tables, dump
> `20260817T172658Z`. See *Record of rehearsals*.

The private key must be on the host, **outside** the release tree, for the same
reason as `rclone.conf` and more urgently: a deleted `rclone.conf` can be
rewritten, a lost private key makes every dump we hold permanently unreadable.

**Transfer it without a `/tmp` hop.** The `install` form below assumes the key is
already on the host, which means putting it somewhere world-readable first. Piping
it straight to its final path never lets it touch disk anywhere else:

```bash
ssh root@HOST 'umask 077
  install -d -m 700 -o root -g root /opt/membership_saas/secrets
  cat > /opt/membership_saas/secrets/age-identity.txt
  chown 1001:1001 /opt/membership_saas/secrets/age-identity.txt
  chmod 600 /opt/membership_saas/secrets/age-identity.txt' < deploy/backup/age-identity.txt
```

Then prove the transfer and the key in one step — `sha256sum` on both sides must
match, and the derived public key must equal `BACKUP_AGE_PUBLIC_KEY` in `.env`:

```bash
docker run --rm --entrypoint age-keygen \
  -v /opt/membership_saas/secrets/age-identity.txt:/k/id.txt:ro \
  membership_saas-backup:latest -y /k/id.txt
```

```bash
# on the host, as root — the key never enters the repo or a release dir
install -m 600 -o 1001 -g 1001 /path/to/age-identity.txt \
  /opt/membership_saas/secrets/age-identity.txt
```

Then in the live `.env`:

```
BACKUP_AGE_IDENTITY_HOST_FILE=/opt/membership_saas/secrets/age-identity.txt
```

and start it:

```bash
docker compose -p membership_saas --profile backup up -d backup-verify
docker compose -p membership_saas logs -f backup-verify
```

The first run happens `BACKUP_VERIFY_INITIAL_DELAY_SECONDS` (default 300s) after
start, so it does not race a dump that is still being written.

### Checking it by hand

```bash
docker compose -p membership_saas --profile backup run --rm \
  --entrypoint ./verify.sh backup-verify
```

```bash
docker exec membership_saas-db-1 psql -U membership_saas -d membership_saas \
  -c 'SELECT verified_at, ok, tables_checked, mismatches, dump_file FROM backup_verifications ORDER BY id DESC LIMIT 5'
```

### Two things it deliberately does

- **Refuses to run if `BACKUP_VERIFY_DB` equals `POSTGRES_DB`.** It drops the
  target database first; that one line pointed at production would be the worst
  outcome in this repo.
- **Excludes `backup_verifications` from the table comparison.** It is the table
  the canary writes, so including it makes every run fail because the previous
  one happened. This was found by the negative pass, not by reading the code.

One accepted false positive: a table that gains its first-ever rows between two
dumps reads as EMPTY once and clears itself on the next dump. The message names
the table. Relaxing the check instead would let a dump that restores with no
data in it pass, which is the whole failure this exists to catch.

## Remote retention

`BACKUP_RETENTION_DAYS` prunes **local** copies inside the container volume
only. Remote copies have two possible mechanisms, and picking the wrong one
for the backend either grows storage forever or deletes backups:

| Backend | Mechanism | Setting |
|---|---|---|
| S3 / R2 / B2 | **Bucket lifecycle policy**, storage-side | leave `BACKUP_REMOTE_RETENTION_DAYS` **unset** |
| Google Drive (current) | `backup.sh` prunes it, because Drive has **no lifecycle policies at all** | `BACKUP_REMOTE_RETENTION_DAYS=30` |

Prefer the lifecycle policy wherever it exists. Storage-side expiry cannot be
broken by a bug in our code — the original, and still correct, reason this
script refused to touch remote objects. That reasoning stopped being
*sufficient* when the target became Drive: deferring to a lifecycle policy on
a backend that has none means remote copies grow until the quota fills and
backups start failing for real.

Where the script must do it, it fails **safe**: deletion is by age via
rclone's own `--min-age` (never a victim list computed by us), `--include`
restricted to our own dump filenames, every deletion logged, and the whole
thing **skipped if the listing is empty or errors** — an unreadable remote
means "we don't know what is out there", never "there is nothing to keep".

A prune problem exits **3**, not 1, and gets its own alert: *"Backup uploaded
OK — but old copies are NOT being deleted"*. Do not collapse that into the
generic failure alert. The backup at that point genuinely succeeded, and an
alert that overstates its case is one people learn to ignore — which is how
the original silent failure survived for weeks.

Keep the remote window at least as long as `BACKUP_RETENTION_DAYS`, so
off-host copies never expire before the local ones.

---

## Record of rehearsals

Keep this table honest — an empty row is better than an assumed one.

| Date | Object restored | Result | By |
|---|---|---|---|
| 2026-08-09 | the dump left on the host by a failed cycle | 30/30 tables, row counts identical to live — but the object came **from the host**, not from storage, because the R2 401 made storage unreachable. Proves dump → encrypt → decrypt → restore only | claude |
| 2026-08-10 | `membership_saas-20260810T125832Z.sql.gz.age`, **pulled down from Google Drive** | 608,764 bytes retrieved (exact match), 8,778,223 uncompressed, restored into scratch Postgres 16.14 — **30/30 tables, every row count identical to live**. Proves the off-host hop end to end | claude |
| 2026-08-18 | `membership_saas-20260817T172658Z.sql.gz.age` — the newest production dump, copied off the host to a machine that is **not** the server, and opened with the key as it stands on the maintainer's PC today | 618,066 bytes (exact match), `age -d` OK, `gzip -t` OK, 7,525 SQL lines / 31 `COPY` blocks, restored into scratch Postgres **16.14** (same minor as prod) with **zero errors** — 31/31 tables present, 4,013 rows. Compared against live: **30/31 identical**; `users` 104 vs 111 because live moved on in the nine hours since the dump, which is the documented non-failure. **No table populated in live and empty in the restore.** Key checked first by derivation: `age-keygen -y` → `age1y46nmqng…`, matching the server's `BACKUP_AGE_PUBLIC_KEY` exactly | claude |
| 2026-08-18 | the same dump, but **on the host, through `verify.sh` itself** — the canary's own first run, immediately after the key was installed | `ok=t`, 30 tables compared, **0 empty in the restore**, 1 moved on since the dump (`users` live=111 restored=104 — the same drift, independently reproduced). Scratch database confirmed dropped (`pg_database` count 0). `assess()` flipped from `state=never` to `state=ok`. This is the on-schedule version of the row above: same dump, same verdict, reached by the machinery rather than by hand | claude |

### Other measurements, 2026-08-10

| What | Result |
|---|---|
| Drive round-trip (write → read → delete) | PASS — 33-byte probe copied, `cat` returned it verbatim, deleted, listing empty after |
| `--s3-no-check-bucket` against a non-S3 backend | PASS — inert, copy exit 0. Verified by running it, not by reasoning |
| Live production upload | PASS — confirmed by a second, independent `lsl` |
| Remote prune at 30 days | PASS — 31/45/90-day objects deleted; 5/13/29 kept; a non-matching 400-day decoy survived |
| Remote prune fail-safe | PASS — forced failure gives exit 3, "nothing was pruned", and the uploaded-but-not-pruning alert |
| Local prune at 14 days (2026-08-09) | PASS — 15/16/30-day dumps deleted, 5/13/14 kept. `-mtime +14` is *strictly older than*, so the real window is 15 days — one more than the name implies, which is the safe direction |
