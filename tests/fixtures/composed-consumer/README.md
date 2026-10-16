# Composed-consumer fixture

Fixed copies of the three passive pack bodies the maintainer's consumers compose into `AGENTS.md`, used by `tests/test_composed_consumer_size.py` to hold a composed consumer under 61,440 bytes without a network fetch. Refresh a copy when its pack body changes on purpose, and record the new size and hash here.

| Pack | Source | Path | Bytes | SHA-256 |
|---|---|---|---|---|
| `agent-style` | github.com/yzhao062/agent-style at `v0.6.0` | `docs/rule-pack-compact.md` | 22,723 | `a41769f8b3ea88b7dc918e8621f52daecc597a474d2a46cfe0b0f84cd7ef9a62` |
| `profile` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-16) | `docs/rule-pack-compact.md` | 4,223 | `8d379dcc50fea12363ae1cd942cc6fd9a9803e08a27cc6cbdd2447a00260e84d` |
| `paper-workflow` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-16) | `docs/paper-workflow-compact.md` | 6,852 | `526313b06e04320c34bf04767f4e550599dcc42fc65fb811e31e689a5a550a1d` |

The composition order is the order consumers use: the bundled `agent-style` first, then the two `agent-pack` packs from `agent-config.yaml`.
