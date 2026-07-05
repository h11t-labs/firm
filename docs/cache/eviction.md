# Eviction & expiry

The cache bounds its own size by evicting entries. Three independent limits, all optional:

| Limit | Default | Evicts when… |
|---|---|---|
| `max_age` | 2 weeks (`1209600.0` s) | an entry is older than this |
| `max_size` | 256 MiB | the estimated total byte size exceeds this |
| `max_entries` | `None` (off) | the row count exceeds this |

Set any to `None` to disable it:

```python
Cache(database_url=..., max_age=3600, max_size=None, max_entries=100_000)
```

`max_age` is also enforced at **read time**: an entry older than `max_age` reads as a miss
(`get` returns `None`, `fetch` recomputes) even if eviction hasn't physically deleted the row
yet — eviction is opportunistic, so an idle or read-heavy cache would otherwise keep serving
stale data.

## How eviction runs

Eviction is **FIFO** — oldest entries (smallest `id`) go first. A run pulls 3× the batch as
candidates and randomly samples the batch from them, so concurrent eviction passes rarely fight over
the same rows.

It's triggered two ways:

- **Probabilistically, on write.** Each `set`/`set_multi` has a small chance (~`2 /
  expiry_batch_size`, so ~2% with the default `expiry_batch_size=100`) of kicking off a background
  eviction run. This keeps eviction pace with writes without running on every one. Disable with
  `auto_expire=False`.
- **On a timer**, if you opt in with `background_expiry=True` (runs every `expiry_interval` seconds).

You can also run a pass yourself:

```python
cache.expiry.run_once()    # -> number of entries evicted
```

## Estimating size cheaply

`max_size` needs to know the cache's total byte size without scanning every row. For small caches
it's an exact `SUM(byte_size)`. Above `size_estimate_samples` (default 10,000) rows it switches to
sampling: it sums the largest rows exactly (the "outliers") and samples a random `key_hash` window —
`key_hash` is uniformly distributed — for the rest, then scales up. The result is a good estimate at
constant cost.

## Tuning

| Parameter | Default | Effect |
|---|---|---|
| `expiry_batch_size` | `100` | Rows removed per eviction run; also sets the ~2/N write-trigger probability. |
| `size_estimate_samples` | `10000` | Exact-sum threshold and sample size for the estimator. |
| `auto_expire` | `True` | Whether writes can trigger eviction. |
| `background_expiry` | `False` | Run a background eviction loop. |
| `expiry_interval` | `60.0` | Seconds between background runs (when enabled). |

> **Note:** with all of `max_age`/`max_size`/`max_entries` disabled, the cache never evicts — it
> grows until you `clear()` or `delete()`.
