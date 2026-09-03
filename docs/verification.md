# Verification

After a round's submission window closes, Minos publishes the round-selection
record, the committed file hashes, and the released round files. These allow
participants to verify selection and file integrity and to reproduce scoring
locally. Miner commitment fields are included when that feature is enabled.

Both endpoints are public, unauthenticated `GET`s:

| Endpoint | Returns |
|---|---|
| `GET /verification/task-window` | Upcoming task hashes, plus recently drawn rounds |
| `GET /verification/round/{round_id}` | One round's draw and `file_hashes`; once the round closes, its `commitments` and `reveal` too |

## Example: the latest revealed round

Run `bash check_round.sh` (repo root) — or `--download DIR` to also fetch and
hash-check the files. Output (`reveal.files` trimmed here; the real response
has all 6 — 3 data files + their `.bai`/`.tbi` indexes):

```json
{
  "round_id": "2026-09-03T13:04:00+00:00",
  "round_status": "scoring",
  "draw": {
    "round_id": "2026-09-03T13:04:00+00:00",
    "block_height": 8986871,
    "block_hash": "74bfbf4291642157a2a31eba0b36b5ccfcb841cb3f0353e198d3ba45c521ab80"
  },
  "file_hashes": {
    "bam": "3f11ac13869a26ad542b38cc138bbdf9efa867dbc246029fcda8eaf70a9ac195",
    "truth_vcf": "c4e5d70d235c65787fde6799aa36d0eb209eabb2646237f3d85f6ea0d3e6a18c",
    "mutations_vcf": "d45241d29c67de30776c585f090e9a6a41266ef3f9cc6a6dc7aaa288c197ae6a"
  },
  "reveal": {
    "bucket": "minos-rounds",
    "prefix": "rounds/20260903T130400Z",
    "files": [
      {"name": "input.bam", "url": "https://rounds.minosproxy.com/rounds/20260903T130400Z/input.bam"},
      {"name": "truth.vcf.gz", "url": "https://rounds.minosproxy.com/rounds/20260903T130400Z/truth.vcf.gz"},
      {"name": "mutations.vcf.gz", "url": "https://rounds.minosproxy.com/rounds/20260903T130400Z/mutations.vcf.gz"}
    ]
  },
  "commitments": 80
}
```

(`check_round.sh` prints `commitments` as a count; the full endpoint response
carries one object per miner — see below. `reveal` and `commitments` appear
once the round's submission window closes, usually within a couple of minutes.
Fields with no value are omitted, so each commitment entry carries only what
was recorded for that miner.)

---

## `draw` — which task the round got

Every round draws one task from a rolling window of ~20 candidates, each
published in advance as a file-hash commitment only. The draw rule:

```
draw_height       = genesis_draw_height + draws_made * blocks_per_round
index             = int(draw_block_hash, 16) % len(candidate_positions)
selected_position = sorted(candidate_positions)[index]
```

Applied independently by [minos-protocol/minos_round_selector](https://github.com/minos-protocol/minos_round_selector)
(MIT, no credentials needed), reading the chain directly:

```bash
git clone https://github.com/minos-protocol/minos_round_selector
cd minos_round_selector && pip install -r requirements.txt

python cli.py verify <round_id>   # one round
python cli.py audit               # every draw on record
python cli.py watch               # as they land
```

Exit codes: `0` verifies, `1` a round contradicts the rule, `2` couldn't
read the API or chain.

---

## Config commitment hashes (`commitments[]`)

One entry per miner who submitted that round. Fields with no value are omitted
from the response, so an entry carries only what was recorded for that miner:

- `config_hash` is present for every submission.
- `miner_commitment` and `miner_commitment_verified` appear when the miner sent
  a commitment with its submission.
- `miner_commitment_block` and `miner_commitment_onchain` appear only once that
  commitment has been located on chain.

Whether miners publish commitments at all is gated on `config_commitment_enabled`
in `/scoring/network-config`.

```json
{
  "hotkey": "5F...",
  "tool_name": "gatk",
  "config_hash": "7f30528831ab480add8c7913c8f0ca36d75ab4b7617c8b33ea5ceed385deec31",
  "miner_commitment": "a1b2c3...",
  "miner_commitment_verified": true
}
```

The miner commitment can be recomputed from the miner's local ledger and
checked against its on-chain record. `config_hash` is a separate commitment
generated for the server-recorded configuration. Because the two commitments
use different constructions and nonces, their hash values are not directly
comparable.

| Field | What it is | How to check it |
|---|---|---|
| `config_hash` | Minos's commitment over the configuration recorded for scoring, salted with its own nonce so the configuration itself stays private | Present for every submission. Verify your own submission through `miner_commitment` below. |
| `miner_commitment` | Your own commitment, built locally with `utils/config_commit.compute_commitment()` and published on chain before submitting (see [docs/miner_features.md](miner_features.md#config-commitment)) | Recompute it from `~/.minos/commitments.jsonl` and compare with the value here and with the on-chain record |
| `miner_commitment_block` | Chain block your commitment landed at | Present once the commitment is located on chain, omitted otherwise. Compare with your ledger and the chain |
| `miner_commitment_verified` | Reported by the API: recomputing your commitment from the config and nonce you sent matches the hash you submitted | You can perform the same recomputation from your ledger |
| `miner_commitment_onchain` | Reported by the API: the hash was found on chain at the named block | Present alongside `miner_commitment_block`, omitted otherwise. Independently checkable against the chain |

Nonces and configurations are not published on this or any endpoint — only
the resulting hashes.

---

## `reveal` — round files

Once a round's submission window closes, Minos releases the committed round
files for independent verification — the same files validators score against.

```bash
curl -O https://rounds.minosproxy.com/rounds/20260903T130400Z/truth.vcf.gz

echo "c4e5d70d235c65787fde6799aa36d0eb209eabb2646237f3d85f6ea0d3e6a18c  truth.vcf.gz" \
  | shasum -a 256 -c -
```

Every URL is a plain unauthenticated `GET` — no presigning, no token. Only
`bam`, `truth_vcf`, and `mutations_vcf` have a published hash in
`file_hashes`; the `.bai`/`.tbi` indexes don't. Get the files for a round from
`/verification/round/{round_id}`; `/verification/task-window` gives you a
`round_id` to start from if you don't have one.

From there, running your submitted tool/config against `input.bam` and
comparing to `truth.vcf.gz` reproduces the scoring locally — the same
comparison practice mode (`--practice`) does for a sample you pick, run here
against a round you actually competed in.
