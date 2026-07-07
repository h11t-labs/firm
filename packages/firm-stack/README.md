# firm-stack

**Pure-Python ports of the Rails Solid stack — database-backed, no Redis required.**

`firm-stack` is the meta-package for [firm](https://github.com/h11t-labs/firm): it installs the
modules you pick via extras, in one shot. Each component is also published independently, so
you can just as well install only what you need:

| Component       | Package                                                  | Ports           |
|-----------------|----------------------------------------------------------|-----------------|
| Background jobs | [`firm-queue`](https://pypi.org/project/firm-queue/)     | Solid Queue     |
| Cache store     | [`firm-cache`](https://pypi.org/project/firm-cache/)     | Solid Cache     |
| Pub/sub         | [`firm-channel`](https://pypi.org/project/firm-channel/) | Solid Cable     |
| Audit log       | [`firm-audit`](https://pypi.org/project/firm-audit/)     | —               |
| Web dashboard   | [`firm-ui`](https://pypi.org/project/firm-ui/)           | Mission Control |

Everything runs on SQLite, PostgreSQL, or MySQL via SQLAlchemy.

## Install

`firm-stack` is extras-only — a bare `pip install firm-stack` installs no modules, so always
pick at least one:

```bash
pip install "firm-stack[queue]"              # background jobs
pip install "firm-stack[queue,cache]"        # jobs + caching
pip install "firm-stack[ui]"                 # web dashboard (pulls all four modules)
pip install "firm-stack[all]"                # everything, all drivers and integrations

# or skip the meta-package and install components directly:
pip install firm-queue
pip install "firm-cache[encryption]"
```

Imports are always under the `firm` namespace regardless of which packages you installed:

```python
import firm.queue
from firm.cache import Cache
from firm.channel import Channel
```

> **Why "firm-stack" and not "firm"?** The PyPI name `firm` is held by a dormant, release-less
> registration; a [PEP 541 name-transfer request](https://github.com/pypi/support/issues/11384)
> is pending. If it is granted, the meta-package will also be published as `firm` and
> `firm-stack` will remain as a compatible alias.

See the [project README and documentation](https://github.com/h11t-labs/firm) for full usage.
