# Composed-consumer fixture

Fixed copies of the three passive pack bodies the maintainer's consumers compose into `AGENTS.md`, used by `tests/test_composed_consumer_size.py` to hold a composed consumer under 61,440 bytes without a network fetch. Refresh a copy when its pack body changes on purpose, and record the new size and hash here.

| Pack | Source | Path | Bytes | SHA-256 |
|---|---|---|---|---|
| `agent-style` | github.com/yzhao062/agent-style at `v0.7.0` | `docs/rule-pack-compact.md` | 22,620 | `56149023fd4ea736bc99a22d964933150fdc61d258e3cfeb5b016be66cfa50bd` |
| `profile` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-17) | `docs/rule-pack-compact.md` | 4,223 | `8d379dcc50fea12363ae1cd942cc6fd9a9803e08a27cc6cbdd2447a00260e84d` |
| `paper-workflow` | github.com/yzhao062/agent-pack (compact bodies, 2026-10-17) | `docs/paper-workflow-compact.md` | 7,012 | `944359e89d38392a54a975c90b6e4a268ec84eec9c2284b6e8fa0b0391fa8dcd` |

The composition order is the order consumers use: the bundled `agent-style` first, then the two `agent-pack` packs from `agent-config.yaml`.
