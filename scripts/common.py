"""Shared by hook.py, ctl.py, selftest.py and hud.py: port, token, one HTTP helper."""
import http.client
import json
import os
import secrets

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 47615
TOKEN_FILE = os.path.join(DIR, "token")
LOG_FILE = os.path.join(DIR, "hud.log")


def token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        t = secrets.token_hex(16)
        with open(TOKEN_FILE, "w") as f:
            f.write(t)
        return t


def call(path, body=None, wait=1.0, tok=None):
    """Connect within 1s (daemon down => fast failure), then wait up to `wait`s for the reply."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
    try:
        c.connect()
        c.sock.settimeout(wait)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        c.request("GET" if body is None else "POST", path, body, {"X-Token": tok or token()})
        r = c.getresponse()
        data = r.read()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return json.loads(data or b"{}")
    finally:
        c.close()
