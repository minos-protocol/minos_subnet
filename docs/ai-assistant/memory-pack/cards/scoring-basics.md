# Minos SN107: Scoring Basics

Memory name: Minos SN107 - Scoring Basics
Version: 1.0.0
Primary subject: Scoring basics
Subjects: Scoring basics; Variant calling; hap.py benchmarking; AdvancedScorer; SNPs and indels; Eligibility and weight
Related memories: Minos SN107 - Scoring And AdvancedScorer; Minos SN107 - Rewards Eligibility And Weight; Minos SN107 - Variant Calling Basics; Minos SN107 - Safe Tuning Workflow

Minos scoring evaluates how well miner-generated variant calls match benchmark truth data during validator scoring.

Core terms:

- True positive: a real variant correctly called.
- False positive: a variant called by the miner that should not be there.
- False negative: a real variant missed by the miner.
- Precision: how many called variants are correct.
- Recall: how many real variants were found.
- F1: a balance between precision and recall.

hap.py-style benchmarking is commonly used to compare called variants against truth data. Minos scoring can combine multiple quality signals, including SNP and indel behavior.

Do not tell miners to "call more variants" as a universal strategy. Overcalling can hurt precision. Over-filtering can hurt recall. Good configs balance quality, completeness, and runtime.

Do not request or disclose unreleased round truth data or other non-public validator materials. Files officially released through the public verification endpoint after a round closes may be used for independent verification.
