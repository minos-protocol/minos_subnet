# Miner features: config commitment and paid resubmission

Two features that do nothing until the platform asks for them. **Neither is
turned on by a local setting** — the platform decides, and one of them can spend
TAO, so read that section before you auto-update.

---

## Config commitment

Your miner can publish a cryptographic commitment to the config it submitted:
on chain (timestamped by block, signed by your hotkey) and alongside the
submission to the platform.

> **Off unless the platform asks for it.** Committing costs one extrinsic per
> submission, so it is enabled network-wide from the platform rather than by
> each operator. While no policy is advertised — which is the case today — your
> miner publishes nothing and submits exactly as before. A platform your miner
> cannot reach also means no commitment, so an outage never starts it spending
> extrinsics.

### What it buys you

The chain commitment is timestamped and signed, so neither side can move
afterwards. The platform cannot claim you sent a different config, and you
cannot claim you sent a better one than you did. The platform runs its own
commitment over the config it **stored**; this is the other half, over the
config as **sent**.

Where the two differ, the difference is the submit-time clamping — which makes
clamping auditable instead of invisible.

### How it works

```
commitment = SHA256(
    domain \x1f version \x1f netuid \x1f round_id \x1f
    hotkey \x1f tool_name \x1f nonce \x1f canonical(config)
)
```

Fields are `\x1f`-separated so no value can be shifted into its neighbour. The
digest binds more than the config on purpose:

| Field | Prevents |
|---|---|
| `domain` | replaying the hash as a different Minos signature |
| `version` | silently reinterpreting old commitments under a new scheme |
| `netuid` | a commitment on one subnet counting on another |
| `round_id` | reusing a commitment for a later round |
| `hotkey` | another miner claiming your published commitment |
| `tool_name` | swapping which tool the config was for |

**A fresh 256-bit nonce per round is required.** Configs come from bounded
parameter ranges, so the input space is small enough to enumerate: without a
nonce, the on-chain digest alone would identify the config it commits to. The
nonce is what stops someone who only reads the chain from opening the payload.
Do not truncate the published digest — any length that still identifies a config
has the same problem, and a short digest makes equivocation cheap.

### Where the nonce goes

It is written to `~/.minos/commitments.jsonl` (mode 0600, fsynced) **before**
anything is published. A commitment whose nonce was lost can never be opened, so
a failed ledger write means no commitment is published at all — not on chain,
not to the platform — and the config is submitted as it would be with the
feature off.

It is then **sent to the platform** with the submission, as `config_nonce`, in
the same request that carries the commitment and the config. The platform needs
it to recompute the digest; without it the platform holds a hash it cannot
check, and the commitment proves nothing to anyone but you.

What that reveals is bounded:

- A nonce opens **exactly one commitment**. The digest is bound to this
  `round_id`, this hotkey, this tool and this config, and a new nonce is drawn
  each round — so the value gives no leverage over any other commitment.
- It reveals **the config in the same request**. The platform is being sent that
  config anyway, so the nonce tells it nothing it does not already have.
- For anyone who only reads the chain, nothing changes: the on-chain payload is
  still a salted digest, and enumerating configs against it needs the nonce.

The nonce is therefore unpredictable rather than secret-forever: it must be
unguessable before you publish, and it is disclosed to the platform with the
config it commits to.

**Back up the ledger.** It is your own copy of what you sent and of the nonce
that opens it, which is what lets you check a commitment without relying on the
platform's record.

| Variable | Default | Effect |
|---|---|---|
| `MINOS_COMMITMENT_LEDGER` | `~/.minos/commitments.jsonl` | Where commitments and nonces are stored |

### Current limits

There is no CLI to open a commitment. `utils/config_commit.verify_commitment()`
recomputes and compares in constant time, but nothing in this repo drives it
from the command line. The ledger holds every input it needs: nonce, the config
as sent, round, hotkey, netuid, tool, and the block the chain write landed at.

The chain write can also be skipped without stopping the submission. Subtensor
enforces a minimum block interval between commitments from one hotkey, so a
rate-limited write is logged and the miner still submits the commitment and
nonce to the platform — you get the platform-side half of that round, not the
timestamped one.

---

## Paid resubmission

> **This feature spends TAO from your coldkey.** Whether resubmissions cost
> anything is decided by the PLATFORM, not by a local switch. While the platform
> advertises no fee — which is the case today — nothing is ever spent. If it
> begins advertising one, a miner that resubmits will pay it unless you have
> opted out. Read "Turning it off" before enabling auto-updates.

### What it is

One free config submission per round **per hotkey**. Beyond that, a submission
requires a small on-chain payment.

The allowance is per hotkey because the hotkey is the registered competitor: it
holds a UID and was paid for at registration. So running a second miner is
already priced by registration, and this fee prices something different — one
miner probing the same round over and over. A miner submitting once per round is
never affected.

The payment must be signed by the **coldkey that owns the submitting hotkey**;
the platform checks that on chain and rejects a payment signed by anything else.
The miner checks the same thing *before* transferring, because a fee refused on
arrival is not refunded.

### The price is not flat

Each paid submission by the same **coldkey** within one round costs more than the
last. The shape, for a base of 0.01 TAO and a step that doubles:

| Paid so far this round | Next submission costs |
|---|---|
| 0 | 0.01 |
| 1 | 0.02 |
| 2 | 0.04 |
| 3 | 0.08 |
| 7 | 1.28 |

The base and the step are the platform's, not the miner's — the table is the
shape, not a quote. The ladder is per coldkey, not per hotkey, so hotkeys
sharing a coldkey advance it together. Your free submissions are unaffected —
one per hotkey, always.

**The miner pays the price the platform quotes.** `round-status` returns
`next_submission_fee_tao`: the cost of *this hotkey's* next paid submission,
already advanced for wherever the coldkey sits on the ladder. The miner records
that quote when it picks up the round and pays exactly it. The base fee in
`/scoring/network-config` is only a fallback, used when no quote came back — an
older platform, or a response without the field. Paying a base fee for anything
past the first paid submission underpays, and underpaying does not fail cheaply:
the transfer is already on chain when the platform rejects it, so the TAO is
gone.

**The per-submission ceiling applies to the quote as well.** A quote above
`MINOS_MAX_RESUBMISSION_FEE_TAO` is refused outright, not clamped, and the miner
then skips the submission rather than sending one that would be rejected as
underpaid. On a doubling ladder that starts at the default `0.01` ceiling, that
allows the round's first paid submission and refuses the second. Raise the
ceiling deliberately if you want more.

One consequence worth knowing: if two of your own hotkeys submit at the same
moment, both are quoted the same price and only one can have it. The other is
rejected as underpaid and loses its fee. Submit sequentially if you run several
hotkeys under one coldkey.

### When this can actually fire

A stock miner submits **once per round and never resubmits**, so the payment path
is not reached in normal operation.

The round loop stops before it: it skips a round it has already submitted to in
this process, and it skips a round for which the platform reports
`has_submitted: true` — logging "already submitted (platform confirmed)" and
returning without running the tool, without paying and without submitting. That
check survives a restart, because the answer comes from the platform rather than
from memory. There is one call site for submission in the miner, reached only
through that gate.

So a fee is only ever considered when the platform reports this hotkey's
submissions-used at or above the free allowance *while still reporting*
`has_submitted: false`. Read the sections below as the bound on what could be
spent if that happens, not as a way to iterate within a round: repeatedly
resubmitting a better config is not something the shipped loop does.

### Making a paid resubmission

The loop never does this on its own. It stops at the first submission of a
round, so a second one is always a deliberate act:

```bash
export MINER_PAY_FOR_RESUBMISSIONS=1
python neurons/miner.py --resubmit
```

Both are required. Without the environment variable nothing is ever paid;
without `--resubmit` the miner sees `has_submitted` and stops before it would
need to pay.

The price is read from the same round response that reports `has_submitted`, so
it already reflects the escalation from every submission your coldkey has made
this round. If the platform quotes no fee, the miner refuses rather than guessing
a price -- an underpayment is refused on arrival and the TAO is already gone.

### Turning it on

This miner spends nothing unless you say so:

```bash
export MINER_PAY_FOR_RESUBMISSIONS=1     # accepts 1, true, yes, on
```

Unset — the default — it never transfers TAO for a submission, whatever the
platform advertises. A submission past the free allowance still goes out, just
without a payment proof; the platform refuses it with HTTP 402, the miner logs
that and carries on to the next round. Nothing is spent either way.

**Why this is opt-in.** `scripts/auto_update.sh` pulls and restarts under pm2,
so a miner acquires new behaviour without anyone reading a release note. If a
published policy were enough to authorise spending, every auto-updated miner
would begin paying the moment one appeared. A ceiling bounds what a mistake
costs; it does not make the spending consented to.

**Set the ceilings anyway.** They bound what a wrong or hostile policy can cost
once you have opted in: a fee above the per-submission cap is refused outright,
and spend is capped over a rolling 24 hours both per hotkey and across every
hotkey sharing this host.

| Variable | Default | Effect |
|---|---|---|
| `MINER_PAY_FOR_RESUBMISSIONS` | *(unset = never spend)* | Set to `1` to take part |
| `MINOS_MAX_RESUBMISSION_FEE_TAO` | `0.01` | Hard per-submission ceiling. A higher fee is **refused, not clamped** |
| `MINOS_MAX_DAILY_RESUBMISSION_TAO` | `0.05` | Hard ceiling on spend **per hotkey** in a rolling 24h |
| `MINOS_MAX_DAILY_WALLET_TAO` | `0.10` | Hard ceiling on spend by **all hotkeys on this host** in a rolling 24h |
| `MINOS_PAYMENT_LEDGER` | `~/.minos/submission_payments.jsonl` | Payment record |
| `MINER_ALLOW_ZERO_FREE_SUBMISSIONS` | *(unset = off)* | Honour an advertised allowance of **zero** free submissions |

An advertised `free_submissions_per_round: 0` would charge for a round's *first*
submission. The miner does not honour that by default: it logs the discrepancy
and uses one free submission instead, unless `MINER_ALLOW_ZERO_FREE_SUBMISSIONS`
is set to `1`/`true`/`yes`.

Worst case with the defaults is `0.05` TAO per hotkey per day, regardless of
what the platform advertises or how many rounds ask. The ceiling is per hotkey
because the allowance is: an operator running several hotkeys on one host would
otherwise share a single budget and silently stop submitting once the busiest
one exhausted it.

### The safety rails

Destination and allowance come from the platform's `/scoring/network-config`, an
**unauthenticated** response. The fee is the `round-status` quote when there is
one — that response is authenticated — and the network-config figure otherwise.
The transfer is signed with your **coldkey**. So:

- **Capped.** A fee above `MINOS_MAX_RESUBMISSION_FEE_TAO` is **refused, not
  clamped**: paying a capped amount toward a bad fee is still paying.
- **Destination validated** as a real ss58 address. A malformed destination is an
  irrecoverable transfer to an account nobody controls.
- **Units guarded.** The transfer refuses outright if the SDK exposes no
  `Balance` type, because a bare float can be read as RAO and a 1e9x transfer
  cannot be undone.
- **Never pays twice.** An unspent proof is reused rather than re-paid, and if a
  transfer's outcome cannot be determined the miner **refuses to retry** and
  tells you to reconcile against the chain.

### Ordering

```
1. record intent locally (fsynced)
2. transfer on chain
3. record proof (fsynced)
4. submit, attaching the proof
5. mark spent only once the PLATFORM ACCEPTS
```

Step 5 follows the platform's verdict, not the HTTP status: a 200 carrying
`success: false` is a rejected submission, and marking it spent there would make
you pay again to retry something you already paid for.

### If something goes wrong

An `ambiguous` transfer outcome leaves an unresolved intent in the ledger, which
blocks further spending on that round. Check the ledger against the chain, then
remove the stale intent once you have settled it.

### Current status

**The fee is switched off, and nothing is charged.** The platform gates it behind
two separate switches — one to publish the policy, one to enforce it — and both
are off. While no policy is advertised, the miner never reads a fee, never
transfers, and submits exactly as it always has.

The server-side half exists: the allowance is counted per hotkey, on-chain proofs
are verified against the chain, and spent references are recorded so a payment
cannot be used twice. None of it has run against a live chain yet.

Expect an announcement before any of this becomes active. You do not need to do
anything to stay out of it: without `MINER_PAY_FOR_RESUBMISSIONS` set, this
miner never pays, regardless of what the platform later advertises.
