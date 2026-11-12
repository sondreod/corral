# scriptrunner-metrics

Report metrics from a script that runs under
[scriptrunner](../README.md), with objects that work like the ones in
`prometheus_client`:

```python
from scriptrunner_metrics import Counter, Gauge, Histogram

JOBS = Counter("jobs_processed", "Jobs processed", ["queue"])
DEPTH = Gauge("queue_depth", "Jobs waiting")
DURATION = Histogram("job_duration_seconds", "Time spent per job")

JOBS.labels(queue="mail").inc()
DEPTH.set(12)
with DURATION.time():
    do_the_job()
```

There is no HTTP server and nothing to scrape in the script. Each call
prints one `@metric …` line on standard output; the runner picks those up,
keeps the values and serves them on its own `/metrics` page with a `script`
label added. Because the runner keeps the values, a counter goes on counting
across the runs of a scheduled script.

Outside scriptrunner the calls do nothing, so a script behaves the same when
you run it by hand. Set `SCRIPTRUNNER_METRICS=1` to see the lines.

No dependencies; Python 3.9 or later.
