# Minos SN107: SNPs And Indels

Memory name: Minos SN107 - SNPs And Indels
Version: 1.0.0
Primary subject: SNPs and indels
Subjects: SNPs and indels; Variant calling; Scoring basics; GATK; DeepVariant; BCFtools
Related memories: Minos SN107 - Variant Calling Basics; Minos SN107 - Scoring Basics; Minos SN107 - Safe Tuning Workflow

A SNP is a single-base change at a genomic position.

An indel is an insertion or deletion relative to the reference genome.

Variant callers often behave differently on SNPs and indels. A configuration that helps SNP recall may not help indel quality, and a strict filter that reduces false positives may also remove true variants.

Minos scoring can reflect multiple quality dimensions. Beginner tuning should avoid chasing one number without checking whether another score component gets worse.

Safe explanation pattern:

- "This setting may affect sensitivity or filtering."
- "Compare several scored rounds before trusting a change."
- "Use public score summaries and public endpoint data."
- "Do not optimize against unreleased truth data; files released after a round closes may be used for verification."
