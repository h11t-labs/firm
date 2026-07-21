# Configuration

Every `Cache(...)` option, with its default:

```python
Cache(
    database_url=None,            # SQLAlchemy URL (or pass engine=)
    engine=None,                  # a pre-built SQLAlchemy Engine, instead of database_url
    coder=None,                   # value coder; defaults to PickleCoder()
    encrypt_key=None,             # Fernet key -> encrypt values at rest
    max_age=1209600.0,            # evict entries older than this many seconds (2 weeks); None = off
    max_size=268435456,           # evict when estimated total bytes exceed this (256 MiB); None = off
    max_entries=None,             # evict when row count exceeds this; None = off
    expiry_batch_size=100,        # rows per eviction run; also sets the write-trigger probability
    max_key_bytesize=1024,        # keys longer than this are truncated + hash-suffixed
    size_estimate_samples=10000,  # exact-sum threshold / sample size for the size estimator
    create_schema=True,           # create firm_cache_entries if missing
    auto_expire=True,             # let writes probabilistically trigger eviction
    background_expiry=False,      # run an eviction loop on a timer
    expiry_interval=60.0,         # seconds between background eviction runs
)
```

| Option | Default | Notes |
|---|---|---|
| `database_url` / `engine` | — | Provide one. `engine` lets two caches share a pool; bare `postgresql://`/`mysql://` URLs are normalized to the shipped drivers. |
| `coder` | `PickleCoder()` | See [Encryption & coders](encryption-and-coders.md). |
| `encrypt_key` | `None` | Fernet key (needs the `[encryption]` extra). |
| `max_age` / `max_size` / `max_entries` | 2 wk / 256 MiB / off | Eviction limits — see [Eviction & expiry](eviction.md). |
| `expiry_batch_size` | `100` | Eviction batch + write-trigger rate (~`2/N`). |
| `max_key_bytesize` | `1024` | Long-key truncation. |
| `size_estimate_samples` | `10000` | Estimator tuning. |
| `create_schema` | `True` | Set `False` if you manage the schema with Alembic. |
| `auto_expire` | `True` | Disable to evict only via `expiry.run_once()` / the background loop. |
| `background_expiry` / `expiry_interval` | `False` / `60.0` | Opt-in timer-based eviction. |
| `on_error` | traceback to stderr | Callback for background-eviction failures (`Exception` -> your handler). |

Call `cache.close()` (or use the `with` form) to stop the background loop and dispose the engine.

## Sharing an engine

```python
from firm._core.database import create_engine_for
engine = create_engine_for("postgresql://localhost/myapp")
cache_a = Cache(engine=engine, max_entries=100_000)
cache_b = Cache(engine=engine, create_schema=False)
```
