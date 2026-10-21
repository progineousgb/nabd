import argparse
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIN_INTERVAL = 15
MAX_INTERVAL = 7 * 24 * 3600
KEEP_CHECKS = 100
TIMEOUT = 10
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
USER_AGENT = "nabd/0.1"


class BindError(ValueError):
    pass


def check_url(url, timeout=TIMEOUT):
    start = time.perf_counter()
    code = None
    error = ""
    up = False
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with _open(req, timeout, url) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            up = 200 <= int(code) < 400
    except urllib.error.HTTPError as exc:
        code = exc.code
        up = False
        try:
            exc.close()
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 - any failure means the target is down
        error = str(exc)[:300]
        up = False
    elapsed = int((time.perf_counter() - start) * 1000)
    return {
        "status_code": code,
        "latency_ms": elapsed,
        "up": up,
        "error": error,
    }


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it points to http or https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlparse(urllib.parse.urljoin(req.full_url, newurl)).scheme.lower()
        if scheme not in {"http", "https"}:
            raise urllib.error.HTTPError(
                req.full_url, code, f"redirect to {scheme or 'unknown'}: refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req, timeout, url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    handlers = [_HttpOnlyRedirects()]
    if host in {"127.0.0.1", "localhost", "::1"}:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR lets a second listener take the same port on Windows.
    allow_reuse_address = sys.platform != "win32"


class Nabd:
    def __init__(self, db_path, host="127.0.0.1", port=8787):
        if host not in LOCAL_HOSTS:
            raise BindError("refusing to bind outside localhost")
        self.db_path = db_path
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.httpd = None
        self._http_thread = None
        self._checker = None
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL").fetchall()
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=3000")
        self._init_db()

    def _init_db(self):
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitors (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    interval_sec INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY,
                    monitor_id INTEGER NOT NULL
                        REFERENCES monitors(id) ON DELETE CASCADE,
                    checked_at INTEGER NOT NULL,
                    status_code INTEGER,
                    latency_ms INTEGER NOT NULL,
                    up INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS checks_by_monitor
                    ON checks(monitor_id, id);
                """
            )
            self.conn.commit()

    def add_monitor(self, name, url, interval):
        name = " ".join(str(name).split())
        url = str(url).strip()
        try:
            interval = int(interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval must be a whole number of seconds") from exc
        if not name:
            raise ValueError("name is required")
        if len(name) > 200:
            raise ValueError("name is too long")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must start with http:// or https://")
        if len(url) > 2000:
            raise ValueError("url is too long")
        if interval < MIN_INTERVAL:
            raise ValueError(f"interval must be at least {MIN_INTERVAL} seconds")
        if interval > MAX_INTERVAL:
            raise ValueError(f"interval must be at most {MAX_INTERVAL} seconds")
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO monitors (name, url, interval_sec) VALUES (?, ?, ?)",
                (name, url, interval),
            )
            self.conn.commit()
            return cur.lastrowid

    def delete_monitor(self, monitor_id):
        with self._lock:
            cur = self.conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def list_monitors(self):
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT m.id, m.name, m.url, m.interval_sec,
                       c.status_code, c.latency_ms, c.up, c.checked_at, c.error
                FROM monitors m
                LEFT JOIN checks c ON c.id = (
                    SELECT id FROM checks
                    WHERE monitor_id = m.id
                    ORDER BY id DESC
                    LIMIT 1
                )
                ORDER BY m.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def render(self):
        return render_page(self.list_monitors())

    def tick(self):
        now = int(time.time())
        with self._lock:
            due = self.conn.execute(
                """
                SELECT m.id, m.url
                FROM monitors m
                LEFT JOIN checks c ON c.id = (
                    SELECT id FROM checks
                    WHERE monitor_id = m.id
                    ORDER BY id DESC
                    LIMIT 1
                )
                WHERE c.id IS NULL OR (? - c.checked_at) >= m.interval_sec
                ORDER BY m.id
                """,
                (now,),
            ).fetchall()
            jobs = [(row["id"], row["url"]) for row in due]
        for monitor_id, url in jobs:
            if self._stop.is_set():
                return
            result = check_url(url)
            with self._lock:
                self.conn.execute(
                    """
                    INSERT INTO checks (
                        monitor_id, checked_at, status_code, latency_ms, up, error
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        monitor_id,
                        int(time.time()),
                        result["status_code"],
                        result["latency_ms"],
                        1 if result["up"] else 0,
                        result["error"],
                    ),
                )
                # Only the latest check is shown, so keep a short tail per monitor.
                self.conn.execute(
                    """
                    DELETE FROM checks
                    WHERE monitor_id = ? AND id <= (
                        SELECT id FROM checks WHERE monitor_id = ?
                        ORDER BY id DESC LIMIT 1 OFFSET ?
                    )
                    """,
                    (monitor_id, monitor_id, KEEP_CHECKS),
                )
                self.conn.commit()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except sqlite3.Error:
                if self._stop.is_set():
                    return
            self._stop.wait(0.4)

    def _start_checker(self):
        if self._checker and self._checker.is_alive():
            return
        self._stop.clear()
        self._checker = threading.Thread(target=self._loop, name="nabd-check", daemon=True)
        self._checker.start()

    def start(self):
        self._bind()
        self._http_thread = threading.Thread(
            target=self.httpd.serve_forever, name="nabd-http", daemon=True
        )
        self._http_thread.start()
        return self.httpd.server_address

    def serve(self):
        self._bind()
        host, port = self.httpd.server_address
        print(f"listening on http://{host}:{port}", flush=True)
        try:
            self.httpd.serve_forever()
        finally:
            self.close()

    def _bind(self):
        if self.httpd is not None:
            return
        self.httpd = _Server((self.host, self.port), self._handler())
        self._start_checker()

    def close(self):
        self._stop.set()
        httpd = self.httpd
        thread = self._http_thread
        if httpd is not None and thread is not None:
            httpd.shutdown()
        if thread is not None:
            thread.join(timeout=5)
            self._http_thread = None
        if httpd is not None:
            httpd.server_close()
            self.httpd = None
        checker = self._checker
        if checker is not None:
            checker.join(timeout=12)
            self._checker = None
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if not _local_host(self.headers):
                    self._send(421, b"unexpected Host header", "text/plain; charset=utf-8")
                    return
                path = urllib.parse.urlparse(self.path).path
                if path == "/favicon.ico":
                    self._send(204, b"")
                    return
                if path != "/":
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                body = app.render().encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")

            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path
                if not _local_host(self.headers):
                    self.close_connection = True
                    self._send(421, b"unexpected Host header", "text/plain; charset=utf-8")
                    return
                if _cross_site(self.headers):
                    self.close_connection = True
                    self._send(403, b"cross-site request rejected", "text/plain; charset=utf-8")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    self.close_connection = True
                    self._send(400, b"bad content length", "text/plain; charset=utf-8")
                    return
                if length < 0 or length > 65536:
                    self.close_connection = True
                    self._send(400, b"request is too large", "text/plain; charset=utf-8")
                    return
                raw = self.rfile.read(length).decode("utf-8", "replace")
                form = urllib.parse.parse_qs(raw, keep_blank_values=True)
                try:
                    if path == "/add":
                        app.add_monitor(
                            _field(form, "name"),
                            _field(form, "url"),
                            _field(form, "interval"),
                        )
                    elif path == "/delete":
                        try:
                            monitor_id = int(_field(form, "id"))
                        except ValueError as exc:
                            raise ValueError("missing monitor id") from exc
                        if not 0 < monitor_id < 2**63 or not app.delete_monitor(monitor_id):
                            self._send(404, b"no such monitor", "text/plain; charset=utf-8")
                            return
                    else:
                        self._send(404, b"not found", "text/plain; charset=utf-8")
                        return
                except ValueError as exc:
                    message = str(exc).encode("utf-8")
                    self._send(400, message, "text/plain; charset=utf-8")
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self._security_headers()
                self.end_headers()

            def _security_headers(self):
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")

            def _send(self, code, body, content_type="text/plain; charset=utf-8"):
                self.send_response(code)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._security_headers()
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        return Handler


def _field(form, name):
    values = form.get(name) or [""]
    return values[0]


def _local_host(headers):
    # A DNS rebinding page reaches us with its own name in Host.
    host = (headers.get("Host") or "").strip().lower()
    if host.startswith("["):
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if ":" in host else host
    return name in LOCAL_HOSTS


def _cross_site(headers):
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site in {"cross-site", "same-site"}:
        return True
    origin = (headers.get("Origin") or "").strip()
    if not origin:
        return False
    host = (headers.get("Host") or "").strip().lower()
    parsed = urllib.parse.urlparse(origin)
    return parsed.scheme != "http" or not host or parsed.netloc.lower() != host


def render_page(rows):
    body = []
    if not rows:
        body.append('<tr><td class="empty" colspan="7">No monitors yet.</td></tr>')
    for row in rows:
        checked = ""
        if row["checked_at"] is None:
            status = '<span class="muted">pending</span>'
            latency = "-"
            checked = "-"
        else:
            code = row["status_code"]
            label = "up" if row["up"] else "down"
            if code is not None:
                label = f"{label} {int(code)}"
            kind = "up" if row["up"] else "down"
            title = escape(row.get("error") or "", quote=True)
            status = f'<span class="{kind}" title="{title}">{label}</span>'
            latency = str(int(row["latency_ms"]))
            checked = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["checked_at"]))
        name = escape(row["name"])
        url = escape(row["url"], quote=True)
        body.append(
            "<tr>"
            f'<td class="name" dir="auto">{name}</td>'
            f'<td class="url" title="{url}"><a href="{url}">{url}</a></td>'
            f"<td>{int(row['interval_sec'])}s</td>"
            f"<td>{status}</td>"
            f"<td>{latency}</td>"
            f"<td>{checked}</td>"
            "<td>"
            '<form method="post" action="/delete">'
            f'<input type="hidden" name="id" value="{int(row["id"])}">'
            '<button type="submit">Delete</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    rows_html = "\n".join(body)
    return f"""<!DOCTYPE html>
<html lang="en" dir="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>nabd</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0;
    background: #121614;
    color: #d5ddd6;
    font: 13px/1.35 system-ui, sans-serif;
  }}
  main {{ max-width: 980px; margin: 0 auto; padding: 20px 16px 32px; }}
  h1 {{ font-size: 16px; font-weight: 600; margin: 0 0 14px; }}
  form.add {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 0 0 14px; }}
  label {{ display: flex; flex-direction: column; gap: 4px; color: #8d9a90; font-size: 12px; }}
  input {{
    background: #1a211c;
    color: #d5ddd6;
    border: 1px solid #2c3830;
    padding: 6px 8px;
    min-width: 0;
  }}
  input[name="url"] {{ width: 280px; }}
  input[name="interval"] {{ width: 88px; }}
  button {{
    background: #243028;
    color: #d5ddd6;
    border: 1px solid #3c4d42;
    padding: 6px 10px;
    cursor: pointer;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #26302a; vertical-align: middle; }}
  th {{ color: #8d9a90; font-weight: 500; font-size: 12px; }}
  td.url {{ max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  td.url a {{ color: inherit; text-decoration: none; }}
  td.url a:hover {{ text-decoration: underline; }}
  td.name {{ unicode-bidi: plaintext; }}
  td.empty {{ color: #8d9a90; }}
  .up {{ color: #3fbf7f; }}
  .down {{ color: #e06a5c; }}
  .muted {{ color: #8d9a90; }}
  form {{ margin: 0; }}
</style>
</head>
<body>
<main>
  <h1>nabd</h1>
  <form class="add" method="post" action="/add">
    <label>name <input name="name" required maxlength="200" dir="auto"></label>
    <label>url <input name="url" required type="url" placeholder="https://example.com"></label>
    <label>interval <input name="interval" required type="number" min="15" max="604800" step="1" value="60"></label>
    <button type="submit">Add</button>
  </form>
  <table>
    <thead>
      <tr>
        <th>name</th>
        <th>url</th>
        <th>interval</th>
        <th>status</th>
        <th>ms</th>
        <th>checked (UTC)</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</main>
</body>
</html>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nabd", description="Local uptime monitor.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default="nabd.db")
    args = parser.parse_args(argv)
    try:
        app = Nabd(args.db, args.host, args.port)
    except BindError as exc:
        print(f"nabd: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"nabd: {exc}", file=sys.stderr)
        return 1
    try:
        app.serve()
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"nabd: {exc}", file=sys.stderr)
        app.close()
        return 1
    return 0
