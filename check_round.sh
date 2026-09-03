#!/usr/bin/env bash
# Fetch a round's draw, config commitments, and revealed files.
# See docs/verification.md for what each field means.
#
# Usage:
#   bash check_round.sh                    # latest revealed round
#   bash check_round.sh <round_id>         # a specific round
#   bash check_round.sh --download DIR     # also download the files and check their hashes
set -euo pipefail

: "${PLATFORM_URL:=https://api.theminos.ai}"

for bin in curl jq; do
  command -v "$bin" >/dev/null || { echo "check_round.sh needs '$bin' installed" >&2; exit 1; }
done

DOWNLOAD_DIR=""
ROUND_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --download)
      [[ $# -ge 2 ]] || { echo "--download needs a directory" >&2; exit 1; }
      DOWNLOAD_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ROUND_ID="$1"; shift ;;
  esac
done

sha256_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

find_latest_revealed() {
  local round_ids rid resp
  round_ids=$(curl -s "$PLATFORM_URL/verification/task-window" \
    | jq -r '[.entries[] | select(.status=="drawn")] | reverse | .[].drawn.round_id')
  for rid in $round_ids; do
    resp=$(curl -s "$PLATFORM_URL/verification/round/$rid")
    if [[ "$(echo "$resp" | jq -r '.reveal // empty')" != "" ]]; then
      echo "$resp"
      return 0
    fi
  done
  echo "No recently drawn round has been revealed yet — try again shortly." >&2
  return 1
}

if [[ -n "$ROUND_ID" ]]; then
  RESPONSE=$(curl -s "$PLATFORM_URL/verification/round/$ROUND_ID")
  [[ "$(echo "$RESPONSE" | jq -r 'has("round_id")')" == "true" ]] || { echo "Round $ROUND_ID not found." >&2; exit 1; }
else
  RESPONSE=$(find_latest_revealed)
fi

echo "$RESPONSE" | jq '{round_id, round_status, draw, file_hashes, reveal, commitments: (.commitments // [] | length)}'

[[ -n "$DOWNLOAD_DIR" ]] || exit 0

REVEAL=$(echo "$RESPONSE" | jq -r '.reveal // empty')
[[ -n "$REVEAL" ]] || { echo "This round has no revealed files yet." >&2; exit 1; }

mkdir -p "$DOWNLOAD_DIR"
echo
echo "Downloading to $DOWNLOAD_DIR ..."
# The name and the URL both come from the API response, so neither is used
# before it is checked. A name containing "/" or ".." would otherwise write
# outside DOWNLOAD_DIR, and a non-HTTPS URL could point anywhere. These are the
# only six names a round publishes; see docs/verification.md.
is_expected_name() {
  case "$1" in
    input.bam|input.bam.bai) return 0 ;;
    truth.vcf.gz|truth.vcf.gz.tbi) return 0 ;;
    mutations.vcf.gz|mutations.vcf.gz.tbi) return 0 ;;
    *) return 1 ;;
  esac
}

# Fed by process substitution rather than a pipe, so the loop runs in this
# shell and can record a failure. Anything unexpected or unfetched is a failure:
# the indexes carry no published digest, so a bad index would otherwise pass
# unnoticed by the hash check below.
status=0
fetched=""
while read -r name url; do
  if ! is_expected_name "$name"; then
    echo "  skipped unexpected filename: $name" >&2
    status=1
    continue
  fi
  if [[ "$url" != https://* ]]; then
    echo "  skipped $name: URL is not https" >&2
    status=1
    continue
  fi
  if curl -sS --fail --proto '=https' --max-time 900 -o "$DOWNLOAD_DIR/$name" "$url"; then
    echo "  fetched $name"
    fetched="$fetched $name"
  else
    echo "  FAILED to fetch $name" >&2
    rm -f "$DOWNLOAD_DIR/$name"
    status=1
  fi
done < <(echo "$RESPONSE" | jq -r '(.reveal.files // [])[] | "\(.name) \(.url)"')

# Checked against what this run fetched, not against what is on disk: a reused
# download directory can still hold a file from an earlier round, and the
# indexes have no published digest to catch that.
for want in input.bam input.bam.bai truth.vcf.gz truth.vcf.gz.tbi mutations.vcf.gz mutations.vcf.gz.tbi; do
  case " $fetched " in
    *" $want "*) ;;
    *) echo "  MISSING $want (not listed by the API, or not fetched this run)" >&2
       status=1 ;;
  esac
done

echo
echo "Checking hashes (bam, truth_vcf, mutations_vcf — indexes have no published hash)..."
for pair in "bam:input.bam" "truth_vcf:truth.vcf.gz" "mutations_vcf:mutations.vcf.gz"; do
  field="${pair%%:*}"; file="${pair##*:}"
  expected=$(echo "$RESPONSE" | jq -r ".file_hashes.$field // empty")
  path="$DOWNLOAD_DIR/$file"
  if [[ -z "$expected" ]]; then
    echo "  NO HASH published for $file"
    status=1
    continue
  fi
  if [[ ! -f "$path" ]]; then
    echo "  MISSING $file (not downloaded)"
    status=1
    continue
  fi
  actual=$(sha256_file "$path")
  if [[ "$actual" == "$expected" ]]; then
    echo "  OK    $file"
  else
    echo "  FAIL  $file (expected $expected, got $actual)"
    status=1
  fi
done

# Exit non-zero on any mismatch or missing file, so this can gate a script.
exit "$status"
