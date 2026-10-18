# Composed-consumer fixture

Fixed copies of the three passive pack bodies the maintainer's consumers compose into `AGENTS.md`, used by `tests/test_composed_consumer_size.py` to hold a composed consumer under 61,440 bytes without a network fetch. Refresh a copy when its pack body changes on purpose, and record the new size and hash here.

| Pack | Source | Path | Bytes | SHA-256 |
|---|---|---|---|---|
| `agent-style` | github.com/yzhao062/agent-style at `v0.9.0` | `docs/rule-pack-compact.md` | 22,544 | `169f0576e4aaf51faae3207ae7ff5ab91c0f4afb862487b120ee9f6e17d3077c` |
| `profile` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-18) | `docs/rule-pack-compact.md` | 4,299 | `fea084a016761ccd183c2906db4cedb770c1ad0bee51eac27b852861ec24b745` |
| `paper-workflow` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-17) | `docs/paper-workflow-compact.md` | 7,012 | `944359e89d38392a54a975c90b6e4a268ec84eec9c2284b6e8fa0b0391fa8dcd` |

The composition order is the order consumers use: the bundled `agent-style` first, then the two `agent-pack` packs from `agent-config.yaml`.
