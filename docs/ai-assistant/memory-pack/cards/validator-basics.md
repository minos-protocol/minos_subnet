# Minos SN107: Validator Basics

Memory name: Minos SN107 - Validator Basics
Version: 1.0.0
Primary subject: Validators
Subjects: Validators; Validator setup; Scoring basics; hap.py benchmarking; AdvancedScorer; Protocol rules
Related memories: Minos SN107 - Validator Guide; Minos SN107 - Validator Safety; Minos SN107 - Scoring And AdvancedScorer; Minos SN107 - Protocol Rules And Rewards

Minos validators score miners by rerunning submitted configs or pipelines and benchmarking the generated calls.

Public validator concepts:

- validators verify miner work instead of trusting miner-provided output
- validator workloads are heavier than normal miner operation
- validators need reliable Docker, CPU, RAM, disk, reference data, and scoring resources
- benchmark comparison uses round truth data that is held back while submissions are open and released publicly after the round closes
- validator weights follow public subnet and scoring rules

Public repo entrypoints may include:

```bash
bash start-validator.sh
bash pm2-validator.sh
```

Use the public repository README and scripts for exact current setup steps.

Do not upload private validator configs, unreleased truth files, confident regions, non-public benchmark data, database output, or infrastructure details into Ditto.
