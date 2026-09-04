# Minos SN107: Safe Tuning Workflow

Memory name: Minos SN107 - Safe Tuning Workflow
Version: 1.0.0
Primary subject: Config tuning
Subjects: Config tuning; How to compete well; Practice mode; Scoring basics; GATK; DeepVariant; BCFtools; Safe support behavior
Related memories: Minos SN107 - Safe Config-Tuning Basics; Minos SN107 - Practice Mode; Minos SN107 - GATK Basics; Minos SN107 - DeepVariant Basics; Minos SN107 - BCFtools Basics; Minos SN107 - Scoring Basics

Tune Minos miners only after the basics work.

Readiness gate:

1. demo mode completes
2. live miner is registered
3. submissions are accepted
4. public scored rounds exist
5. the miner understands which score component or failure mode is being targeted

Safe workflow:

1. Change one category at a time.
2. Record the exact change.
3. Score the change in practice mode against the same sample as the baseline.
4. Run enough live rounds to confirm the behavior holds.
5. Watch precision, recall, completeness, runtime, and reliability.
6. Revert changes that improve one number while breaking the workflow.

Practice mode is where a change should be measured first. It prints the exact score a validator would compute, under the scoring version the platform advertises, so a config can be rejected before a live round pays for it. Keep the sample fixed with `--sample-id` when comparing two configs, or the sample difference explains the score difference.

Do not promise guaranteed winning configs. Do not tune from unreleased truth data. Do not ask users to paste private configs. For live score and history, use Minos MCP or public endpoints.
