# firm

**Pure-Python ports of the Rails Solid stack — database-backed, no Redis required.**

`firm` is a meta-package that installs the full stack in one shot. Each component is also
published independently, so you can install only what you need:

| Component       | Package                                                  | Ports           |
|-----------------|----------------------------------------------------------|-----------------|
| Background jobs | [`firm-queue`](https://pypi.org/project/firm-queue/)     | Solid Queue     |
| Cache store     | [`firm-cache`](https://pypi.org/project/firm-cache/)     | Solid Cache     |
| Pub/sub         | [`firm-channel`](https://pypi.org/project/firm-channel/) | Solid Cable     |
| Audit log       | [`firm-audit`](https://pypi.org/project/firm-audit/)     | —               |
| Web dashboard   | [`firm-ui`](https://pypi.org/project/firm-ui/)           | Mission Control |

Everything runs on SQLite, PostgreSQL, or MySQL via SQLAlchemy.

## Install

```bash
pip install firm            # the four core modules (queue, cache, channel, audit)
pip install "firm[ui]"      # + the web dashboard
pip install "firm[all]"     # everything, all drivers and integrations

# or just one component:
pip install firm-queue
pip install "firm-cache[encryption]"
```

Imports are always under the `firm` namespace regardless of which packages you installed:

```python
import firm.queue
from firm.cache import Cache
from firm.channel import Channel
```

See the [project README and documentation](https://github.com/h11t-labs/firm) for full usage.
