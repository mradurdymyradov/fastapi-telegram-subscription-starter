#!/usr/bin/env bash
# GK-427 / GK-441: does this copy of the age private key actually open our backups?
#
# There have been two keys in this project. One works. The other failed its own
# checksum on 2026-08-09, which meant every dump taken until then was permanently
# unreadable while the nightly job reported success throughout.
#
# The comment line inside an identity file is NOT proof. The 09.08 key carried a
# perfectly well-formed `# public key: age1t6twp5z9…` header above a secret that
# does not parse. Only derivation proves anything, which is what this does.
#
# Use it on any copy whose provenance you are not certain of — the one in the
# password manager, the one the client is handed at transfer, the one you just
# found on an old laptop.
#
#   ./check-identity.sh /path/to/age-identity.txt
#   ./check-identity.sh /path/to/age-identity.txt age1<expected-public-key>
#
# With no expected key given it falls back to BACKUP_AGE_PUBLIC_KEY from the
# environment, then to the recipient recorded below.

set -euo pipefail

# The live recipient. Every dump from 2026-08-09 17:39 UTC onward is encrypted to
# this. Public keys are not secret — this is the whole point of age.
LIVE_RECIPIENT="age1y46nmqngus75xuexxrwcjgdqdu0y7ce9c5fursfxjm08xfa5eynsus90h8"
# The 2026-08-09 corrupt key, named so a match reports the specific failure
# rather than a generic mismatch.
DEAD_RECIPIENT="age1t6twp5z9ucmhz552jdgfvl8acslpa5vywtdukq92k8aeuf3e74aq9st2rk"

usage() { echo "usage: $0 <identity-file> [expected-public-key]" >&2; exit 64; }

[ $# -ge 1 ] || usage
IDENTITY="$1"
EXPECTED="${2:-${BACKUP_AGE_PUBLIC_KEY:-$LIVE_RECIPIENT}}"

[ -f "$IDENTITY" ] || { echo "FAIL: no such file: $IDENTITY" >&2; exit 1; }

if ! command -v age-keygen >/dev/null 2>&1; then
    cat >&2 <<-MSG
	FAIL: age-keygen is not on PATH.

	Run it through the backup image instead - same binary, no install.
	Pipe the file in rather than bind-mounting it; a Windows host path in
	a -v flag is a coin flip, stdin is not:

	  docker run --rm -i --entrypoint sh membership_saas-backup:latest \
	    -c 'cat > /tmp/id.txt; ./check-identity.sh /tmp/id.txt' < "$IDENTITY"

	That prints the same verdict as this script. To check a copy straight out
	of a password manager without ever writing it to disk, copy it to the
	clipboard and pipe that in - PowerShell:

	  Get-Clipboard -Raw | docker run --rm -i --entrypoint sh \
	    membership_saas-backup:latest -c 'cat > /tmp/id.txt; ./check-identity.sh /tmp/id.txt'

	Expected public key:
	  $EXPECTED
	MSG
    exit 69
fi

# Normalise before deriving. A copy that arrives from a password manager via a
# Windows clipboard carries a UTF-8 BOM and CRLF line endings; the BOM lands in
# front of the `#` on line 1, age stops seeing a comment, tries to parse the
# header as a secret and reports `malformed secret key: mixed case`. That reads
# exactly like a corrupt key and is not one — the wrong verdict in the one place
# a wrong verdict is expensive. Strip both, then derive.
NORMALISED="$(mktemp)"
trap 'rm -f "$NORMALISED"' EXIT
if [ "$(head -c 3 "$IDENTITY" | od -An -tx1 | tr -d ' \n')" = "efbbbf" ]; then
    tail -c +4 "$IDENTITY" | tr -d '\r' > "$NORMALISED"
else
    tr -d '\r' < "$IDENTITY" > "$NORMALISED"
fi

# Derivation is the test. A corrupt secret fails here with `malformed secret key:
# invalid checksum` no matter how convincing the file's header comment looks.
if ! DERIVED="$(age-keygen -y "$NORMALISED" 2>&1)"; then
    echo "FAIL: this file is not a usable age identity."
    echo "      $DERIVED"
    echo
    case "$DERIVED" in
        *"invalid checksum"*)
            echo "      A well-formed key that fails its own checksum is exactly the"
            echo "      2026-08-09 failure: it looks like a key, it is not one. If this"
            echo "      copy were the only one, every dump encrypted to it would be"
            echo "      unreadable, permanently." ;;
        *)
            echo "      This does not parse as an age identity at all — check you copied"
            echo "      the whole file, including the AGE-SECRET-KEY-1… line." ;;
    esac
    exit 1
fi

if [ "$DERIVED" = "$EXPECTED" ]; then
    echo "PASS: this key opens our backups."
    echo "      derived  $DERIVED"
    exit 0
fi

echo "FAIL: this is a real age key, but the WRONG one."
echo "      derived  $DERIVED"
echo "      expected $EXPECTED"
if [ "$DERIVED" = "$DEAD_RECIPIENT" ]; then
    echo
    echo "      That is the retired pre-2026-08-09 key. It cannot open any dump"
    echo "      taken since. Replace this copy with the live identity."
fi
exit 1
