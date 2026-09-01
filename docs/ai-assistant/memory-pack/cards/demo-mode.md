# Minos SN107: Demo Mode

Memory name: Minos SN107 - Demo Mode
Version: 1.0.0
Primary subject: Demo mode
Subjects: Demo mode; Practice mode; Miner onboarding; Docker runtime; Runtime operations; Troubleshooting
Related memories: Minos SN107 - Practice Mode; Minos SN107 - Miner Lifecycle; Minos SN107 - Hardware And Runtime Guide; Minos SN107 - Troubleshooting Playbook; Minos SN107 - Live Mining And Registration

Demo mode is the beginner-safe first test for Minos.

Use demo mode before live mining because it checks that the local machine can run the miner workflow without risking live subnet confusion. It helps confirm dependencies, Docker behavior, file handling, and the selected variant-calling tool path.

Demo mode does not persist live submissions, does not earn TAO, and does not produce real live scores.

Demo mode is the self-scorer pinned to one fixed chr20 sample, so the first run is repeatable. It downloads that sample's BAM and truth, runs the selected caller, and prints the exact score a validator would compute for that sample, while recording nothing on-chain. Practice mode is the same self-scorer with a sample menu; use it once demo works and the question becomes which config scores better.

Like practice mode, demo mode asks the platform which scorer the network is using and scores with that formula, naming the version in its output. With the platform unreachable it uses the version the machine last resolved, and v1 if it never resolved one.

Demo mode is useful when:

- the miner was just installed
- Docker or dependencies were recently changed
- a new tool path is being tested
- the miner cannot produce a result
- the user is unsure whether the problem is local runtime or live registration

Demo mode does not prove that a live miner is registered, eligible, weighted, or earning. After demo mode works, use practice mode to compare configs, then move to live setup and inspect public live status through Minos MCP or public endpoints.
