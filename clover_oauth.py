"""Server-side Clover sandbox authorization and token refresh."""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.cookies import SimpleCookie, CookieError

AUTHORIZE = "https://sandbox.dev.clover.com/oauth/v2/authorize"
API = "https://apisandbox.dev.clover.com"
COOKIE = "clover_connect_state"
FORM_COOKIE = "clover_connect_form"
ID = re.compile(r"[A-Za-z0-9]{1,64}")
TOKEN_LOCK = threading.Lock()


class OAuthError(Exception):
    pass


class CloverOAuth:
    def __init__(self, db, staff_code, secure):
        self.db = db
        self.staff_code = staff_code
        self.secure = secure
        self.app_id = os.environ.get("CLOVER_LOYALTY_APP_ID", "")
        self.secret = os.environ.get("CLOVER_OAUTH_APP_SECRET", "")
        self.origin = os.environ.get("CLOVER_OAUTH_BASE_URL", "").rstrip("/")
        parsed = urllib.parse.urlparse(self.origin)
        if (not ID.fullmatch(self.app_id) or not self.secret or parsed.scheme != "https"
                or not parsed.netloc or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Set CLOVER_LOYALTY_APP_ID, CLOVER_OAUTH_APP_SECRET and an HTTPS CLOVER_OAUTH_BASE_URL.")
        self.callback = self.origin + "/clover/callback"

    def init_db(self):
        with closing(self.db()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS clover_oauth_states(
                    digest TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS clover_oauth_tokens(
                    merchant TEXT PRIMARY KEY, access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL, access_expires INTEGER NOT NULL,
                    refresh_expires INTEGER NOT NULL);
            """)

    def post(self, endpoint, body):
        req = urllib.request.Request(API + endpoint, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "StevesCoffeeRewards/1.0"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                data = json.load(response)
            now = int(time.time())
            if (not isinstance(data, dict)
                    or any(not isinstance(data.get(key), str) or not data[key]
                           for key in ("access_token", "refresh_token"))
                    or any(type(data.get(key)) is not int or data[key] <= now
                           for key in ("access_token_expiration", "refresh_token_expiration"))):
                raise OAuthError("Clover did not return a valid expiring token pair.")
            return data
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise OAuthError("Could not complete Clover authorization. Start the connection again.") from None

    def save(self, merchant, pair):
        with closing(self.db()) as conn, conn:
            conn.execute("""
                INSERT INTO clover_oauth_tokens VALUES (?,?,?,?,?)
                ON CONFLICT(merchant) DO UPDATE SET
                access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                access_expires=excluded.access_expires, refresh_expires=excluded.refresh_expires
            """, (merchant, pair["access_token"], pair["refresh_token"],
                  pair["access_token_expiration"], pair["refresh_token_expiration"]))

    def access_token(self, merchant):
        with TOKEN_LOCK:
            with closing(self.db()) as conn:
                row = conn.execute("SELECT * FROM clover_oauth_tokens WHERE merchant=?", (merchant,)).fetchone()
            if row is None or row["refresh_expires"] <= int(time.time()):
                raise OAuthError("Reconnect the sandbox merchant at /clover/connect.")
            if row["access_expires"] > int(time.time()) + 60:
                return row["access_token"]
            pair = self.post("/oauth/v2/refresh", {
                "client_id": self.app_id, "refresh_token": row["refresh_token"]})
            self.save(merchant, pair)
            return pair["access_token"]

    def cookie(self, handler, name=COOKIE):
        cookies = SimpleCookie()
        try:
            cookies.load(handler.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookies[name].value if name in cookies else ""

    def cookie_header(self, value="", age=600, name=COOKIE):
        suffix = "; Secure" if self.secure else ""
        return f"{name}={value}; Path=/clover; HttpOnly; SameSite=Lax; Max-Age={age}{suffix}"

    def page(self, handler, status, message, form=False, nonce=""):
        content = f'<p role="status">{html.escape(message)}</p>'
        if form:
            content += f"""
                <form method="post" action="/clover/connect">
                  <input name="nonce" type="hidden" value="{html.escape(nonce)}">
                  <label for="code">Staff code</label>
                  <input id="code" name="code" type="password" autocomplete="off" required>
                  <button class="btn block" style="margin-top:16px">Connect Clover sandbox</button>
                </form>"""
        body = f"""<!doctype html><html lang="en"><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <meta name="robots" content="noindex"><title>Steve’s Coffee | Clover connection</title>
            <link rel="stylesheet" href="/style.css"><main>
            <div class="card"><h1>Connect Clover</h1>{content}</div></main></html>""".encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Security-Policy", "default-src 'self'; form-action 'self' https://sandbox.dev.clover.com; frame-ancestors 'none'")
        if form:
            handler.send_header("Set-Cookie", self.cookie_header(nonce, 600, FORM_COOKIE))
        handler.end_headers()
        handler.wfile.write(body)

    def redirect(self, handler, url, state):
        handler.send_response(303)
        handler.send_header("Location", url)
        handler.send_header("Set-Cookie", self.cookie_header(state, 600 if state else 0))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def form_nonce(self, handler):
        existing = self.cookie(handler, FORM_COOKIE)
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", existing):
            return existing
        return secrets.token_urlsafe(32)

    def handle(self, handler, method, path):
        if path == "/clover/connect" and method == "GET":
            return self.page(handler, 200,
                "Enter the staff code to authorize a Clover test merchant.", True,
                self.form_nonce(handler))
        if path == "/clover/connect" and method == "POST":
            if handler.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise OAuthError("Use the Clover connection form.")
            try:
                body = urllib.parse.parse_qs(handler.read_body().decode(), max_num_fields=4)
            except (ValueError, UnicodeError):
                raise OAuthError("Invalid connection form.") from None
            nonce = body.get("nonce", [""])[0]
            code = body.get("code", [""])[0].strip()
            cookie = self.cookie(handler, FORM_COOKIE)
            if not nonce or not cookie or not hmac.compare_digest(nonce.encode(), cookie.encode()):
                return self.page(handler, 400,
                    "Your connection form expired. Enter your staff code again below. If this repeats, allow cookies for this site.",
                    True, self.form_nonce(handler))
            if not self.staff_code or not hmac.compare_digest(code.encode(), self.staff_code.encode()):
                return self.page(handler, 401, "Check your staff code and try again.", True, nonce)
            state = secrets.token_urlsafe(32)
            with closing(self.db()) as conn, conn:
                conn.execute("DELETE FROM clover_oauth_states WHERE expires <= ?", (int(time.time()),))
                conn.execute("INSERT INTO clover_oauth_states VALUES (?,?)",
                             (hashlib.sha256(state.encode()).hexdigest(), int(time.time()) + 600))
            query = urllib.parse.urlencode({"client_id": self.app_id, "redirect_uri": self.callback,
                                           "response_type": "code", "state": state})
            return self.redirect(handler, AUTHORIZE + "?" + query, state)
        if path == "/clover/callback" and method == "GET":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query, max_num_fields=12)
            except ValueError:
                raise OAuthError("Invalid Clover response.") from None
            state = query.get("state", [""])[0]
            cookie = self.cookie(handler)
            if not state or not cookie or not hmac.compare_digest(state.encode(), cookie.encode()):
                raise OAuthError("Connection session expired. Start again at /clover/connect.")
            with closing(self.db()) as conn, conn:
                taken = conn.execute("DELETE FROM clover_oauth_states WHERE digest=? AND expires>?",
                    (hashlib.sha256(state.encode()).hexdigest(), int(time.time())))
                if taken.rowcount != 1:
                    raise OAuthError("Connection session expired. Start again at /clover/connect.")
            merchant = query.get("merchant_id", [""])[0]
            app_id = query.get("client_id", [self.app_id])[0]
            code = query.get("code", [""])[0]
            if (query.get("error") or not ID.fullmatch(merchant)
                    or app_id != self.app_id or not code or len(code) > 4096):
                raise OAuthError("Clover authorization was cancelled or incomplete.")
            pair = self.post("/oauth/v2/token",
                {"client_id": self.app_id, "client_secret": self.secret, "code": code})
            self.save(merchant, pair)
            return self.redirect(handler, "/clover/connected", "")
        if path == "/clover/connected" and method == "GET":
            return self.page(handler, 200,
                "The Clover authorization step is complete. Configure the sandbox merchant ID and webhooks in Render to test automatic points.")
        return self.page(handler, 405, "Use the Clover connection page to continue.")
