# nabd

nabd is a local uptime monitor for a few URLs. One process checks each URL on an interval, stores the result in a SQLite file, and serves a status page on your computer.

## What you get

A page at http://127.0.0.1:8787. The page is a form plus a table. You add a name, an `http` or `https` URL, and how often to check it. After a check, the row shows the status, the HTTP code, the latency in milliseconds, and the time of that check in UTC.

## Install and run

nabd needs Python 3.11 or newer. It uses the standard library only, so no install step is needed. From this directory:

Linux and macOS:

```text
python3 -m nabd
```

Windows (PowerShell or cmd):

```text
py -m nabd
```

On macOS, install Python 3.11 or newer from python.org or Homebrew (`brew install python`) if `python3 --version` is older. On Windows, install it from python.org and tick "Add python.exe to PATH".

It prints:

```text
listening on http://127.0.0.1:8787
```

Leave that process running and open the URL in a browser on the same computer. Stop the process when you are done; it does not daemonize. Checks and monitors are stored in `nabd.db` in the working directory, so the next start loads the same list.

The same defaults, written out:

```text
python -m nabd --db nabd.db --host 127.0.0.1 --port 8787
```

## Example

This was a local smoke run of the server on http://127.0.0.1:8787. The database was outside the repo, and the process was stopped at the end.

Before anything was added, the table body was the text `No monitors yet.`

The form has three fields:

- `name`, required, at most 200 characters
- `url`, required, and it must start with `http://` or `https://`
- `interval`, required, a whole number of seconds, from 15 to 604800 (one week). The box starts at 60

Submit runs `POST /add`. Each existing row has a Delete button, which posts that row's id to `POST /delete`.

The smoke run added name `local`, url `http://127.0.0.1:50481/`, and interval `15`. That URL was a temporary local process that answered `200` with the body `ok`. After the check, the row was:

```text
name      local
url       http://127.0.0.1:50481/
interval  15s
status    up 200
ms        41
checked   2026-10-21 12:22:42
```

`up 200` is the status cell: the target was up, and the HTTP code was 200. `41` is the latency in milliseconds. `2026-10-21 12:22:42` is the check time in UTC. The page's time column is headed `checked (UTC)`.

## Safety

The status page sends `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`, so another site cannot load it in a frame. A check follows a redirect only when it points to `http` or `https`. A redirect to any other scheme, such as `file:` or `ftp:`, is not followed and the target is reported as down.

## Flags and files

```text
python -m nabd [--host 127.0.0.1] [--port 8787] [--db nabd.db]
```

- `--host` defaults to `127.0.0.1`. `localhost` is the other accepted value. Any other host is refused, the process exits 2, and the database file is not created.
- `--port` is the listen port. The default is 8787, so the page is http://127.0.0.1:8787.
- `--db` is the SQLite file. The default is `nabd.db` in the current directory.

`GET /` is the status page. `GET /favicon.ico` returns 204. Anything else returns 404. The HTML includes `<meta http-equiv="refresh" content="15">`, so the browser reloads the page every 15 seconds and a later check shows up without a manual reload.

A check is an HTTP GET. nabd will check any address you give it, including ones on your local network, so only add URLs you mean to poll. nabd waits at most 10 seconds. A status from 200 through 399 is up, and the cell reads `up` plus the code, as in `up 200`. An HTTP error status is down plus that code. When the connection fails, the cell reads `down`, there is no code, and the status element title holds the error text. The `ms` column is how long that GET took. The clock column is UTC, formatted `YYYY-MM-DD HH:MM:SS`. Until the first check finishes, the status cell reads `pending` and the latency and time cells are `-`.

A name is stored as UTF-8. The page sets `dir="auto"` on the name field and on the name cell.

From this directory, the tests are:

```text
python -m unittest discover -s tests -v
```

## Limits

The page shows only the latest check for each monitor. nabd has no accounts and no alerts. A failed check changes the row on this page and does nothing else: no mail, no webhook, no log line.

The listener stays on localhost, and a request whose `Host` header is not `127.0.0.1` or `localhost` gets 421, so a DNS rebinding page cannot read or change the list. A form post whose `Origin` or `Sec-Fetch-Site` marks it as cross-site is rejected with 403. The shortest interval is 15 seconds. Names over 200 characters and URLs over 2000 characters are rejected. Only the last 100 checks per monitor are kept in `nabd.db`. Stopping the process leaves the file in place.
