# Variant Calling Primer For Minos Miners

This primer gives a Minos support agent enough public genomics context to help beginners.

## Core Terms

BAM:

An aligned read file. It contains short DNA fragments placed against a reference genome. This is the evidence variant callers inspect.

VCF:

A variant call file. It lists predicted SNPs, indels, genotypes, quality values, and filters.

Reference genome:

The baseline genome sequence used for alignment and comparison.

Read:

A short piece of sequenced DNA. Many reads overlap each genomic position.

SNP:

A single-base substitution.

Indel:

An insertion or deletion.

False positive:

A variant the caller reports that should not be there.

False negative:

A real variant the caller missed.

Precision:

Of called variants, how many were correct.

Recall:

Of real variants, how many were found.

F1:

A balance between precision and recall.

## What Minos Rewards

Minos rewards useful variant calling. Calling more variants is not automatically better.

A strong miner balances:

- weighted F1
- completeness
- false-positive control
- quality
- runtime reliability
- valid submissions over time

## Caller Notes

### GATK

GATK HaplotypeCaller is highly configurable. It assembles candidate haplotypes and evaluates read evidence.

Common tuning categories:

- base-quality thresholds
- mapping-quality thresholds
- calling confidence
- emit/reference confidence behavior
- PCR indel model
- graph pruning
- haplotype population limits
- active region thresholds
- assembly region size and padding
- contamination filtering
- soft-clipped base handling

Good practice:

- tune one category at a time
- watch false positives and recall together
- avoid extreme permissive configs until the miner is reliable
- keep runtime and memory in mind

### DeepVariant

DeepVariant uses a learned model and is usually strong with fewer knobs.

Common issues:

- RAM pressure
- disk pressure
- Docker image pull/runtime failures
- longer runtime
- timeout risk

Good practice:

- use DeepVariant only on machines with enough RAM
- treat it as a stable caller, not a small-knob tuning surface

### BCFtools

BCFtools is fast and lightweight.

Common uses:

- smaller machines
- learning the pipeline
- fast smoke tests

Good practice:

- do not assume speed means best score
- monitor quality and false positives

### hap.py

hap.py compares calls to truth and reports metrics. Miners should understand the concepts, but they should not request unreleased truth data or other non-public validator materials. Files officially released through the public verification endpoint after a round closes may be used for independent verification.

## Diagnosing Score Shape

Low recall:

- The caller may be too strict.
- It may miss indels or low-quality evidence.
- Do not simply loosen everything.

High false positives:

- The caller may be too permissive.
- Quality/confidence filters may be weak.
- Overcalling can hurt score.

Low completeness:

- Output may be missing regions.
- Tool may have timed out or crashed.
- Reference/region compatibility may be wrong.

Low quality:

- Calls may be low confidence.
- Filtering may be weak.

Runtime instability:

- Tool may be too heavy for the VM.
- Docker, disk, RAM, and timeout are first-class scoring risks.

## Safe Experiment Pattern

1. Pick a baseline.
2. Record selected caller and config.
3. Run multiple rounds.
4. Identify one target weakness.
5. Change one parameter category.
6. Compare public scores over multiple rounds.
7. Keep only changes that improve the target without breaking other components.

Never promise a winning config.
