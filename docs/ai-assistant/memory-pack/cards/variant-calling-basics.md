# Minos SN107: Variant Calling Basics

Memory name: Minos SN107 - Variant Calling Basics
Version: 1.0.0
Primary subject: Variant calling
Subjects: Variant calling; BAM files; VCF files; Reference genome and reads; SNPs and indels; Scoring basics
Related memories: Minos SN107 - BAM VCF Reference And Reads; Minos SN107 - SNPs And Indels; Minos SN107 - Scoring Basics; Minos SN107 - GATK Basics; Minos SN107 - DeepVariant Basics; Minos SN107 - BCFtools Basics

Variant calling is the process of finding differences between sequencing reads and a reference genome.

In Minos, a miner's tool or pipeline reads genomic data and produces variant calls. A validator benchmarks those calls against truth data to judge quality.

Important beginner ideas:

- Reads are short pieces of DNA sequencing evidence.
- A reference genome is the coordinate system used for comparison.
- A BAM file usually stores aligned sequencing reads.
- A VCF file stores called variants.
- A good variant caller balances finding true variants with avoiding false calls.

Better mining does not mean producing more variants. Good results usually balance recall, precision, completeness, quality filtering, and runtime.

Do not paste private BAM files, VCF files, unreleased truth files, confident regions, presigned URLs, or private challenge data into Ditto, public issues, or public support channels.
