import http.client
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nabd.server import Nabd, check_url, main


ROOT = Path(__file__).resolve().parents[1]


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "nabd.db")

    def test_refuses_public_bind(self):
        code = main(["--host", "0.0.0.0", "--db", self.db, "--port", "0"])
        self.assertEqual(code, 2)
        self.assertFalse(Path(self.db).exists())

    def test_connection_refused_is_down(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = check_url(f"http://127.0.0.1:{port}/")
        self.assertFalse(result["up"])
        self.assertIsNone(result["status_code"])
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_unicode_name_on_dashboard(self):
        app = Nabd(self.db)
        self.addCleanup(app.close)
        app.add_monitor("الخدمة", "http://127.0.0.1/health", 30)
        html = app.render()
        self.assertIn("الخدمة", html)
        self.assertIn('dir="auto"', html)
        self.assertIn('charset="utf-8"', html)

    def test_check_persists_and_delete(self):
        target = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        url = f"http://127.0.0.1:{target.server_address[1]}/health"

        app = Nabd(self.db, port=0)
        host, port = app.start()
        self.addCleanup(app.close)
        base = f"http://{host}:{port}"
        opener = _opener()

        self._post(
            opener,
            base + "/add",
            {"name": "local", "url": url, "interval": "15"},
        )
        row = self._wait_check(app)
        self.assertEqual(row["up"], 1)
        self.assertEqual(row["status_code"], 200)
        self.assertGreaterEqual(row["latency_ms"], 0)

        page = self._get(opener, base + "/")
        self.assertIn("local", page)
        self.assertIn("up 200", page)
        self.assertIn('class="up"', page)

        app.close()
        conn = sqlite3.connect(self.db)
        stored = conn.execute("SELECT up, status_code FROM checks").fetchone()
        conn.close()
        self.assertEqual(tuple(stored), (1, 200))

        again = Nabd(self.db, port=0)
        host, port = again.start()
        self.addCleanup(again.close)
        base = f"http://{host}:{port}"
        page = self._get(opener, base + "/")
        self.assertIn("local", page)
        monitor_id = again.list_monitors()[0]["id"]
        self._post(opener, base + "/delete", {"id": str(monitor_id)})
        page = self._get(opener, base + "/")
        self.assertNotIn("local", page)
        self.assertEqual(again.list_monitors(), [])

        again.close()
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM monitors").fetchone()[0]
        checks = conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(checks, 0)

    def test_rejects_short_interval(self):
        app = Nabd(self.db, port=0)
        host, port = app.start()
        self.addCleanup(app.close)
        opener = _opener()
        try:
            self._post(
                opener,
                f"http://{host}:{port}/add",
                {"name": "too fast", "url": "http://127.0.0.1/", "interval": "5"},
            )
            self.fail("expected interval to be rejected")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn(b"15", exc.read())
        self.assertEqual(app.list_monitors(), [])

    def test_second_listener_is_refused(self):
        app = Nabd(self.db, port=0)
        host, port = app.start()
        self.addCleanup(app.close)
        other = Nabd(str(Path(self.tmp.name) / "other.db"), host=host, port=port)
        self.addCleanup(other.close)
        with self.assertRaises(OSError):
            other.start()

    def test_rejects_cross_site_post(self):
        app = Nabd(self.db, port=0)
        host, port = app.start()
        self.addCleanup(app.close)
        conn = http.client.HTTPConnection(host, port, timeout=5)
        body = urllib.parse.urlencode(
            {"name": "x", "url": "http://127.0.0.1/", "interval": "30"}
        )
        conn.request(
            "POST",
            "/add",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        self.assertEqual(app.list_monitors(), [])
        conn.close()

    def test_rejects_bad_content_length(self):
        app = Nabd(self.db, port=0)
        host, port = app.start()
        self.addCleanup(app.close)
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/add",
            body=b"name=a",
            headers={"Content-Length": "nope"},
        )
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)
        self.assertEqual(app.list_monitors(), [])
        conn.close()

    def test_module_serves_page(self):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "nabd",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--db",
                self.db,
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(self._stop_process, proc)
        line = self._readline(proc, 8)
        match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
        self.assertIsNotNone(match, line)
        page = self._get(_opener(), f"http://127.0.0.1:{match.group(1)}/")
        self.assertIn("nabd", page)
        self.assertIn("No monitors yet.", page)

    def test_foreign_host_header_is_refused(self):
        app = Nabd(self.db, port=0)
        host, port = app.start()
        self.addCleanup(app.close)
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/", headers={"Host": f"rebind.example:{port}"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 421)
        conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)

    def test_huge_numbers_are_rejected(self):
        app = Nabd(self.db)
        self.addCleanup(app.close)
        with self.assertRaises(ValueError):
            app.add_monitor("big", "http://127.0.0.1/", "9" * 30)

    def test_old_checks_are_pruned(self):
        app = Nabd(self.db)
        self.addCleanup(app.close)
        monitor_id = app.add_monitor("gone", "http://127.0.0.1:9/", 15)
        with app._lock:
            app.conn.executemany(
                "INSERT INTO checks (monitor_id, checked_at, latency_ms, up) VALUES (?, 0, 0, 0)",
                [(monitor_id,)] * 300,
            )
            app.conn.commit()
        app.tick()
        count = app.conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        self.assertLessEqual(count, 101)

    def _post(self, opener, url, fields):
        data = urllib.parse.urlencode(fields).encode()
        return opener.open(urllib.request.Request(url, data=data), timeout=5)

    def _get(self, opener, url):
        deadline = time.time() + 5
        last = None
        while time.time() < deadline:
            try:
                with opener.open(url, timeout=2) as resp:
                    return resp.read().decode("utf-8")
            except urllib.error.URLError as exc:
                last = exc
                time.sleep(0.05)
        raise AssertionError(last)

    def _wait_check(self, app):
        deadline = time.time() + 10
        while time.time() < deadline:
            rows = app.list_monitors()
            if rows and rows[0]["checked_at"] is not None:
                return rows[0]
            time.sleep(0.05)
        self.fail("monitor was not checked")

    def _readline(self, proc, timeout):
        box = []

        def read():
            box.append(proc.stdout.readline())

        thread = threading.Thread(target=read)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self._stop_process(proc)
            self.fail("server did not print a listen line")
        return box[0]

    def _stop_process(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
