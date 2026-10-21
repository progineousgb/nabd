nabd is a local uptime monitor. One process stores results in `nabd.db` in the working directory and serves a status page.

Run from this directory:

```
python -m nabd
```

The page is at http://127.0.0.1:8787. Add a name, an `http` or `https` URL, and an interval of at least 15 seconds. The table shows the last status, latency in milliseconds, and the check time in UTC. Delete removes a monitor. Each check waits at most 10 seconds. A network error counts as down.

The process binds to localhost only. There are no accounts. Form posts from another site are rejected.

Names are stored as UTF-8. The page sets `dir="auto"` on name fields so right-to-left text keeps its order.

```
python -m nabd --db nabd.db --port 8787
```

Tests:

```
python -m unittest discover -s tests -v
```
