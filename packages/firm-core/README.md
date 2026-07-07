# firm-core

Shared internal infrastructure for the [firm](https://github.com/h11t-labs/firm) packages:
engine/connection handling, SQL dialect seams, the interruptible poller, process registry, and
configuration plumbing.

**You probably don't want to install this directly.** It is pulled in automatically by the
packages that use it:

```bash
pip install firm-queue    # background jobs   (port of Solid Queue)
pip install firm-cache    # caching           (port of Solid Cache)
pip install firm-channel  # pub/sub           (port of Solid Cable)
pip install firm-audit    # append-only audit log
```

Database drivers are exposed here as extras and reachable from every module, e.g.
`pip install "firm-queue[postgres]"` resolves to `firm-core[postgres]`.

The internal API (`firm._core`) is private and may change between minor releases; depend on the
public modules instead.

## Docs

- [firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed; see [NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE) for third-party
notices.
