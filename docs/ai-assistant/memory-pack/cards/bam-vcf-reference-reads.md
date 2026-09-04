# Minos SN107: BAM, VCF, Reference, And Reads

Memory name: Minos SN107 - BAM VCF Reference And Reads
Version: 1.0.0
Primary subject: Variant calling
Subjects: Variant calling; BAM files; VCF files; Reference genome and reads; Safe paste-back
Related memories: Minos SN107 - Variant Calling Basics; Minos SN107 - SNPs And Indels; Minos SN107 - Safe Paste-Back Template; Minos SN107 - Validator Safety

Minos miners should understand these file concepts at a high level.

Reads are sequencing fragments. They are the raw evidence a variant caller uses.

A reference genome is the baseline genome sequence. Reads are aligned to reference coordinates so tools can compare sample evidence against the reference.

BAM files usually contain aligned reads. BAM files can be large and may reveal private or challenge-specific data. Do not paste private BAM content or private BAM URLs into public support.

VCF files contain variant calls such as SNPs and indels. Miner support should focus on the official miner flow and public score output, not manual VCF paste-back.

Truth VCFs and confident-region files are validator-side benchmark materials while a round is open. Do not request or disclose unreleased round truth data or other non-public validator materials, and do not upload them into a public knowledge graph. Files officially released through the public verification endpoint after a round closes may be used for independent verification.

Public-safe support can discuss what these file types mean, common error categories, and public logs after secrets and private paths are removed.
