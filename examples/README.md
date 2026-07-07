# firm examples

Runnable scripts. Each creates a local SQLite database (`firm-*.db`) for the demo.

| File | What it shows | Run |
|---|---|---|
| [quickstart.py](quickstart.py) | queue + cache + channel + audit in one script | `uv run python examples/quickstart.py` |
| [combined_worker.py](combined_worker.py) | a job that reads/writes the cache and broadcasts completion on a channel | `uv run python examples/combined_worker.py` |
| [embedded_worker.py](embedded_worker.py) | running the queue *inside* your app process with `ThreadSupervisor` | `uv run python examples/embedded_worker.py` |
| [audit_logging.py](audit_logging.py) | same-transaction audit events, a queue job's lifecycle, and `history()` querying | `uv run python examples/audit_logging.py` |
| [fastapi_app.py](fastapi_app.py) | a FastAPI app (lifespan + enqueue + read-through cache) | `uv run uvicorn examples.fastapi_app:app` |
| [flask_app.py](flask_app.py) | a Flask app (`Firm` extension + enqueue + cache) | `uv run flask --app examples.flask_app run` |
| [secured_dashboard.py](secured_dashboard.py) | the firm-ui dashboard behind Basic auth + a custom Authenticator | `uv run python examples/secured_dashboard.py` |

Install what each needs, e.g. `pip install "firm[queue,fastapi]"` or `pip install "firm[queue,flask]"`.
These demos create the schema directly for convenience; production uses the bundled Alembic
migrations (see [Database backends](../docs/database-backends.md)).

For running the queue as a separate scalable worker deployment (fork mode, containers, Kubernetes),
see the [Deployment guide](../docs/deployment.md).
