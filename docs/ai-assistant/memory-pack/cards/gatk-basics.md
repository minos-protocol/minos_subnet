# Minos SN107: GATK Basics

Memory name: Minos SN107 - GATK Basics
Version: 1.0.0
Primary subject: GATK
Subjects: GATK; Variant calling; Config tuning; Scoring basics; How to compete well
Related memories: Minos SN107 - Safe Tuning Workflow; Minos SN107 - Safe Config-Tuning Basics; Minos SN107 - Scoring Basics; Minos SN107 - SNPs And Indels

GATK is a powerful and tunable variant-calling toolkit. In Minos, GATK can be useful for miners who understand how filter and assembly settings affect output quality.

Beginner-safe GATK tuning categories:

- base quality filters
- mapping quality filters
- confidence thresholds
- PCR indel model
- pruning parameters
- haplotype and assembly limits
- active region thresholds
- contamination filtering
- soft-clipped base handling

GATK tuning should be measured across multiple scored rounds. A single round can be noisy or unrepresentative.

Do not present magic winning values. Do not optimize against unreleased truth data. Do not copy configs from private validator material. Explain tradeoffs and require public score evidence.
