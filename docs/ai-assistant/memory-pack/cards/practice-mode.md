# Minos SN107: Practice Mode

Memory name: Minos SN107 - Practice Mode
Version: 1.0.0
Primary subject: Practice mode
Subjects: Practice mode; Demo mode; Miner onboarding; Config tuning; Scoring basics; Runtime operations
Related memories: Minos SN107 - Demo Mode; Minos SN107 - Miner Lifecycle; Minos SN107 - Safe Tuning Workflow; Minos SN107 - Scoring Basics; Minos SN107 - Troubleshooting Playbook

Practice mode is the self-scoring sandbox for Minos. It is how a miner measures a config before a live round pays for it.

```bash
bash start-miner.sh --practice
```

Practice mode picks a fully-answered sample from the platform, downloads its BAM plus truth plus mutations files, runs the selected variant caller, and prints the exact score a validator would compute for that config.

## What Practice Mode Is

- One-shot and interactive. It scores and exits; it is not a long-running process and should not run under PM2.
- No wallet, no hotkey, no registration, no chain. It uses an ephemeral keypair against the platform's practice namespace.
- Samples cover chr18, chr19, chr20, chr21, and chr22. Live rounds rotate over the set in `chromosome_rotation` in `/scoring/network-config`, which is wider than the practice set.
- Sample files land in `datasets/practice/<sample_id>/` and are reused on later runs instead of re-downloaded.
- The printed result includes SNP and indel F1, recall, precision, and false-positive counts, the advanced score out of 100, and the combined final score a validator would record. A run that called nothing on target is flagged as zero-input, which a validator discards.
- Two scoring versions exist; the active one is published in network configuration (`scoring_version` in `/scoring/network-config`) and applied consistently by validators. Practice reads it and scores with that formula, and the printed result names the version. If network configuration cannot be reached it falls back to the version the machine last resolved, and to v1 if it never has — that fallback can put the number on a different scale from the network's.

## What Practice Mode Is Not

Practice mode does not submit anything, does not earn TAO, does not appear on the leaderboard, and does not count toward eligibility. Only live scored rounds do that. A miner who practices all day still has zero valid scored rounds.

## Practice Versus Demo

Demo mode (`--demo`) is the same self-scorer pinned to one fixed chr20 sample, so a brand-new operator gets a repeatable first score in one command. Practice mode scores against any of the available samples and lets the operator choose the tool and config. Run demo first to prove the pipeline, then practice to compare configs.

## Useful Flags

```bash
bash start-miner.sh --practice --config configs/gatk.conf
bash start-miner.sh --practice --config configs/gatk.conf --sample-id <sample-id>
bash start-miner.sh --practice --miner-template deepvariant
```

With no flags the run is interactive: choose score or download, confirm the variant caller, then pick a sample. Passing `--config` or `--sample-id` skips the menus for scripted comparisons.

## Requirements

Practice mode needs Docker and platform connectivity. The first run fetches the reference FASTA and the RTG SDF for the practice chromosomes, roughly 60 MB and 24 MB per chromosome. Start practice through `start-miner.sh` rather than calling the Python module directly, because the script is what fetches those assets.

## Safety Note

Practice truth files are delivered to the operator's machine on purpose so configs can be compared offline. They still must not be pasted into chat, uploaded, or seeded into a knowledge graph. Public support should ask for the printed score summary, not the files.
