#!/usr/bin/env python3
"""Steve's Coffee: order-ahead + loyalty (V1). Python stdlib only.

Run:  python3 server.py        then open http://localhost:8000
Staff screen: http://localhost:8000/staff (set STAFF_PIN first)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import clover

BASE = Path(__file__).parent
STATIC = BASE / "static"
TEMPORARY_DB = os.environ.get("TEMPORARY_DB") == "1"
DEFAULT_DB_PATH = "/tmp/steves.db" if TEMPORARY_DB else str(BASE / "steves.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

CFG = {
    "shop_name": os.environ.get("SHOP_NAME", "Steve's Coffee"),
    "open": os.environ.get("STORE_OPEN", "06:30"),
    "close": os.environ.get("STORE_CLOSE", "18:00"),
    "prep_minutes": int(os.environ.get("PREP_MINUTES", "10")),
    "slot_minutes": 5,
    "slot_capacity": int(os.environ.get("SLOT_CAPACITY", "5")),
    "curbside_enabled": os.environ.get("CURBSIDE_ENABLED", "0") == "1",
    "always_open": os.environ.get("ALWAYS_OPEN", "0") == "1",  # demo/testing only
    "ordering_enabled": os.environ.get("ORDERING_ENABLED") == "1",
    "points_per_dollar": 1,
    "welcome_bonus": 10,
}
STAFF_PIN = os.environ.get("STAFF_PIN", "").strip()
PILOT_CODE = os.environ.get("PILOT_CODE", "")
ON_RENDER = os.environ.get("RENDER") == "true"
PILOT_COOKIE = "steves_pilot"
PILOT_PROOF = hashlib.sha256(("steves-pilot:" + PILOT_CODE).encode()).hexdigest()
SESSION_DAYS = 7

REWARDS = [
    {"id": "drink", "name": "Free handcrafted drink", "cost": 100, "category": "drink"},
    {"id": "beans", "name": "Free bag of beans", "cost": 200, "category": "beans"},
]
REWARDS_BY_ID = {r["id"]: r for r in REWARDS}

MENU = json.loads((BASE / "menu.json").read_text())
ITEMS = {i["id"]: i for i in MENU["items"]}

ACTIVE = ("received", "preparing", "ready")
STATUSES = ACTIVE + ("picked_up", "cancelled")
LOCK = threading.Lock()
CLOVER = clover.get_client()
FAILED_LOGINS = {}  # phone -> [timestamps]


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


# ---------- database ----------

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS customers(
            phone TEXT PRIMARY KEY, name TEXT NOT NULL, pin_salt TEXT, pin_hash TEXT,
            points INTEGER NOT NULL DEFAULT 0, created TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, phone TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, delta INTEGER,
            reason TEXT, order_id INTEGER, ts TEXT);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT UNIQUE,
            phone TEXT, name TEXT, lines TEXT, subtotal_cents INTEGER,
            discount_cents INTEGER, total_cents INTEGER, reward_id TEXT,
            reward_cost INTEGER DEFAULT 0, pickup_time TEXT, pickup_type TEXT,
            vehicle TEXT, arrived INTEGER DEFAULT 0, status TEXT,
            points_awarded INTEGER DEFAULT 0, clover_id TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS receipt_credits(
            receipt TEXT PRIMARY KEY, phone TEXT NOT NULL,
            subtotal_cents INTEGER NOT NULL, points INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)


def get_setting(con, key, default=""):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def stamp():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def add_points(con, phone, delta, reason, order_id=None):
    con.execute("UPDATE customers SET points = points + ? WHERE phone=?", (delta, phone))
    con.execute("INSERT INTO ledger(phone,delta,reason,order_id,ts) VALUES(?,?,?,?,?)",
                (phone, delta, reason, order_id, stamp()))


# ---------- auth ----------

def clean_phone(p):
    d = re.sub(r"[^0-9]", "", str(p or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        raise ApiError(400, "Enter a valid 10-digit phone number.")
    return d


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100_000).hex()


def session_cutoff():
    return (datetime.now() - timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")


def new_session(con, phone):
    con.execute("DELETE FROM sessions WHERE created < ?", (session_cutoff(),))
    tok = secrets.token_urlsafe(24)
    con.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, phone, stamp()))
    return tok


def customer_from(con, headers):
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    r = con.execute("SELECT c.* FROM sessions s JOIN customers c ON c.phone=s.phone "
                    "WHERE s.token=? AND s.created>=?", (auth[7:], session_cutoff())).fetchone()
    return r


def public_customer(c):
    return {"name": c["name"], "phone": c["phone"], "points": c["points"]}


def require_staff(headers):
    encoded = headers.get("X-Staff-Code")
    if encoded is None:
        code = headers.get("X-Staff-Pin", "")
    else:
        try:
            code = unquote(encoded, errors="strict")
        except UnicodeError:
            raise ApiError(401, "Wrong staff code.") from None
    if not STAFF_PIN or not hmac.compare_digest(code.strip().encode(), STAFF_PIN.encode()):
        raise ApiError(401, "Wrong staff code.")


# ---------- pickup slots ----------

def parse_hm(s, day):
    h, m = s.split(":")
    return day.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def compute_slots(con):
    if not CFG["ordering_enabled"]:
        return {"open": False, "paused": True, "reason": "Online ordering is on hold.", "slots": []}
    now = datetime.now()
    open_t, close_t = parse_hm(CFG["open"], now), parse_hm(CFG["close"], now)
    if CFG["always_open"]:
        open_t, close_t = now - timedelta(hours=1), now + timedelta(hours=3)
    step = CFG["slot_minutes"]
    first = max(now + timedelta(minutes=CFG["prep_minutes"]), open_t)
    first = first.replace(second=0, microsecond=0)
    first += timedelta(minutes=(-first.minute) % step)
    counts = {r["pickup_time"]: r["n"] for r in con.execute(
        "SELECT pickup_time, COUNT(*) n FROM orders WHERE status IN (?,?,?) GROUP BY pickup_time", ACTIVE)}
    slots, t = [], first
    while t < close_t:
        iso = t.strftime("%Y-%m-%dT%H:%M")
        slots.append({"time": iso, "label": t.strftime("%-I:%M %p"),
                      "available": counts.get(iso, 0) < CFG["slot_capacity"]})
        t += timedelta(minutes=step)
    paused = get_setting(con, "paused", "0") == "1"
    reason = ""
    if paused:
        reason = "We're slammed right now and have paused online orders. Please check back soon!"
    elif not slots:
        reason = f"We're closed for today. Online ordering reopens at {open_t.strftime('%-I:%M %p')}."
    return {"open": bool(slots) and not paused, "paused": paused, "reason": reason, "slots": slots}


# ---------- pricing ----------

def price_lines(raw_lines):
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiError(400, "Your cart is empty.")
    lines, subtotal = [], 0
    if len(raw_lines) > 40:
        raise ApiError(400, "Too many items in one order.")
    for ln in raw_lines:
        if not isinstance(ln, dict) or not isinstance(ln.get("id"), str):
            raise ApiError(400, "Invalid cart item.")
        item = ITEMS.get(ln["id"])
        if not item:
            raise ApiError(400, "That item is no longer on the menu.")
        try:
            qty = int(ln.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if not 1 <= qty <= 20:
            raise ApiError(400, "Quantity must be between 1 and 20.")
        mods, unit, desc = ln.get("mods") or {}, item["price"], []
        if not isinstance(mods, dict):
            raise ApiError(400, "Invalid item options.")
        for gid in item["groups"]:
            g = MENU["modifier_groups"][gid]
            opts = {o["id"]: o for o in g["options"]}
            sel = mods.get(gid)
            if g["multi"]:
                sel = sel or []
                if not isinstance(sel, list) or any(not isinstance(s, str) or s not in opts for s in sel):
                    raise ApiError(400, f"Invalid {g['label'].lower()} choice.")
                chosen = list(dict.fromkeys(sel))
            else:
                sel = sel or g.get("default")
                if sel is None:
                    if g["required"]:
                        raise ApiError(400, f"Please choose a {g['label'].lower()}.")
                    chosen = []
                elif not isinstance(sel, str) or sel not in opts:
                    raise ApiError(400, f"Invalid {g['label'].lower()} choice.")
                else:
                    chosen = [sel]
            for oid in chosen:
                unit += opts[oid]["price"]
                desc.append(opts[oid]["name"])
        lines.append({"id": item["id"], "name": item["name"], "category": item["category"],
                      "qty": qty, "unit_cents": unit, "desc": desc,
                      "mods": {k: mods[k] for k in item["groups"] if k in mods}})
        subtotal += unit * qty
    return lines, subtotal


def apply_reward(lines, subtotal, reward_id, customer):
    if not reward_id:
        return 0, None
    r = REWARDS_BY_ID.get(reward_id) if isinstance(reward_id, str) else None
    if not r:
        raise ApiError(400, "Unknown reward.")
    if not customer:
        raise ApiError(401, "Sign in to use rewards.")
    if customer["points"] < r["cost"]:
        raise ApiError(400, f"You need {r['cost']} points for that reward.")
    eligible = [l["unit_cents"] for l in lines if l["category"] == r["category"]]
    if not eligible:
        raise ApiError(400, f"Add a {r['category']} to your cart to use this reward.")
    return max(eligible), r


def serialize_order(o, staff=False):
    d = {
        "id": o["public_id"], "name": o["name"], "lines": json.loads(o["lines"]),
        "subtotal_cents": o["subtotal_cents"], "discount_cents": o["discount_cents"],
        "total_cents": o["total_cents"], "reward_id": o["reward_id"],
        "pickup_time": o["pickup_time"], "pickup_type": o["pickup_type"],
        "arrived": bool(o["arrived"]), "status": o["status"],
        "points_awarded": o["points_awarded"], "created": o["created"],
        "pickup_label": datetime.strptime(o["pickup_time"], "%Y-%m-%dT%H:%M").strftime("%-I:%M %p"),
        "has_account": bool(o["phone"]),
    }
    if staff:
        d["vehicle"] = o["vehicle"]
        d["phone_last4"] = (o["phone"] or "")[-4:]
        d["clover_id"] = o["clover_id"]
    return d


# ---------- handlers ----------

def h_config(ctx):
    with db() as con:
        s = compute_slots(con)
    return {"shop_name": CFG["shop_name"], "curbside_enabled": CFG["curbside_enabled"],
            "rewards": REWARDS, "welcome_bonus": CFG["welcome_bonus"],
            "ordering_enabled": CFG["ordering_enabled"], "points_per_dollar": CFG["points_per_dollar"],
            "ordering": {k: s[k] for k in ("open", "paused", "reason")},
            "clover": CLOVER.name}


def h_menu(ctx):
    return MENU


def h_slots(ctx):
    with db() as con:
        return compute_slots(con)


def h_signup(ctx):
    b = ctx["body"]
    phone, name, pin = clean_phone(b.get("phone")), str(b.get("name", "")).strip()[:40], str(b.get("pin", ""))
    if not name:
        raise ApiError(400, "Please enter your name.")
    if not re.fullmatch(r"[0-9]{4}", pin):
        raise ApiError(400, "PIN must be 4 digits.")
    with LOCK, db() as con:
        if con.execute("SELECT 1 FROM customers WHERE phone=?", (phone,)).fetchone():
            raise ApiError(409, "That number already has an account. Try signing in.")
        salt = secrets.token_hex(16)
        con.execute("INSERT INTO customers(phone,name,pin_salt,pin_hash,points,created) VALUES(?,?,?,?,0,?)",
                    (phone, name, salt, hash_pin(pin, salt), stamp()))
        add_points(con, phone, CFG["welcome_bonus"], "Welcome bonus")
        tok = new_session(con, phone)
        c = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
    return {"token": tok, "customer": public_customer(c)}


def h_login(ctx):
    b = ctx["body"]
    phone, pin = clean_phone(b.get("phone")), str(b.get("pin", ""))
    if not re.fullmatch(r"[0-9]{4}", pin):
        raise ApiError(401, "Wrong phone number or PIN.")
    now = datetime.now().timestamp()
    with LOCK, db() as con:
        for key, attempts in list(FAILED_LOGINS.items()):
            if now - attempts[-1] >= 600:
                del FAILED_LOGINS[key]
        recent = [t for t in FAILED_LOGINS.get(phone, []) if now - t < 600]
        if len(recent) >= 5 or (not recent and len(FAILED_LOGINS) >= 2048):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        c = con.execute("SELECT * FROM customers WHERE phone=?",
                        (phone,)).fetchone()
        ok = c and hmac.compare_digest(hash_pin(pin, c["pin_salt"]), c["pin_hash"])
        if not ok:
            FAILED_LOGINS[phone] = recent + [now]
            raise ApiError(401, "Wrong phone number or PIN.")
        FAILED_LOGINS.pop(phone, None)
        return {"token": new_session(con, phone), "customer": public_customer(c)}


def h_logout(ctx):
    auth = ctx["headers"].get("Authorization", "")
    if auth.startswith("Bearer "):
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token=?", (auth[7:],))
    return {"signed_out": True}

def h_me(ctx):
    with db() as con:
        c = customer_from(con, ctx["headers"])
        if not c:
            raise ApiError(401, "Please sign in.")
        orders = con.execute("SELECT * FROM orders WHERE phone=? ORDER BY id DESC LIMIT 10", (c["phone"],)).fetchall()
        ledger = con.execute("SELECT delta,reason,ts FROM ledger WHERE phone=? ORDER BY id DESC LIMIT 15", (c["phone"],)).fetchall()
    return {"customer": public_customer(c), "orders": [serialize_order(o) for o in orders],
            "ledger": [dict(r) for r in ledger]}


def h_quote(ctx):
    if not CFG["ordering_enabled"]:
        raise ApiError(409, "Online ordering is on hold.")
    b = ctx["body"]
    lines, subtotal = price_lines(b.get("lines"))
    with db() as con:
        c = customer_from(con, ctx["headers"])
    discount, reward = apply_reward(lines, subtotal, b.get("reward_id"), c)
    return {"lines": lines, "subtotal_cents": subtotal, "discount_cents": discount,
            "total_cents": subtotal - discount,
            "points_to_earn": ((subtotal - discount) // 100) * CFG["points_per_dollar"] if c else 0}


def h_create_order(ctx):
    b = ctx["body"]
    with LOCK, db() as con:
        slots = compute_slots(con)
        if not slots["open"]:
            raise ApiError(409, slots["reason"])
        c = customer_from(con, ctx["headers"])
        if not c and ctx["headers"].get("Authorization"):
            raise ApiError(401, "Your session expired. Sign in again before ordering.")
        name = (c["name"] if c else str(b.get("name", ""))).strip()[:40]
        if not name:
            raise ApiError(400, "Please enter a name for the order.")
        lines, subtotal = price_lines(b.get("lines"))
        discount, reward = apply_reward(lines, subtotal, b.get("reward_id"), c)

        want = b.get("pickup_time", "asap")
        avail = [s for s in slots["slots"] if s["available"]]
        if want == "asap":
            slot = avail[0] if avail else None
        else:
            slot = next((s for s in avail if s["time"] == want), None)
        if not slot:
            raise ApiError(409, "That pickup time just filled up. Please pick another.")

        ptype = b.get("pickup_type", "walkin")
        vehicle = str(b.get("vehicle", "")).strip()[:60]
        if ptype == "curbside":
            if not CFG["curbside_enabled"]:
                raise ApiError(400, "Curbside pickup isn't available.")
            if not vehicle:
                raise ApiError(400, "Tell us what you're driving so we can find you.")
        else:
            ptype, vehicle = "walkin", ""

        public_id = secrets.token_hex(16).upper()
        cur = con.execute(
            "INSERT INTO orders(public_id,phone,name,lines,subtotal_cents,discount_cents,total_cents,"
            "reward_id,reward_cost,pickup_time,pickup_type,vehicle,status,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (public_id, c["phone"] if c else None, name, json.dumps(lines), subtotal, discount,
             subtotal - discount, reward["id"] if reward else None, reward["cost"] if reward else 0,
             slot["time"], ptype, vehicle, "received", stamp()))
        oid = cur.lastrowid
        if reward:
            add_points(con, c["phone"], -reward["cost"], f"Redeemed: {reward['name']}", oid)
        row = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        order = serialize_order(row, staff=True)
        order["public_id"] = public_id
        order["discount_cents"] = discount
        order["total_cents"] = subtotal - discount
        try:
            con.execute("UPDATE orders SET clover_id=? WHERE id=?", (CLOVER.submit_order(order), oid))
        except Exception as e:  # never lose an order because the POS hiccuped
            print(f"[clover] sync failed for {public_id}: {e}")
            con.execute("UPDATE orders SET clover_id='SYNC_FAILED' WHERE id=?", (oid,))
        row = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return serialize_order(row)


def get_order(con, public_id):
    o = con.execute("SELECT * FROM orders WHERE public_id=?", (public_id.upper(),)).fetchone()
    if not o:
        raise ApiError(404, "Order not found.")
    return o


def customer_order(con, ctx, public_id):
    o = get_order(con, public_id)
    if o["phone"]:
        customer = customer_from(con, ctx["headers"])
        if not customer or customer["phone"] != o["phone"]:
            raise ApiError(404, "Order not found.")
    elif len(o["public_id"]) != 32:
        raise ApiError(404, "Order not found.")
    return o


def h_get_order(ctx, public_id):
    with db() as con:
        return serialize_order(customer_order(con, ctx, public_id))


def h_arrived(ctx, public_id):
    with LOCK, db() as con:
        o = customer_order(con, ctx, public_id)
        if o["pickup_type"] != "curbside" or o["status"] not in ACTIVE:
            raise ApiError(400, "This isn't a curbside order.")
        con.execute("UPDATE orders SET arrived=1 WHERE id=?", (o["id"],))
        return serialize_order(get_order(con, public_id))


def h_staff_member(ctx):
    require_staff(ctx["headers"])
    phone = clean_phone(ctx["body"].get("phone"))
    with db() as con:
        member = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
    if not member:
        raise ApiError(404, "No rewards account found. Ask the customer to join first.")
    return {"customer": public_customer(member)}


def h_staff_credit(ctx):
    require_staff(ctx["headers"])
    body = ctx["body"]
    phone = clean_phone(body.get("phone"))
    receipt = str(body.get("receipt") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,59}", receipt):
        raise ApiError(400, "Enter a receipt reference using letters, numbers, dashes, or underscores.")
    amount = str(body.get("subtotal") or "").strip()
    if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,2})?", amount):
        raise ApiError(400, "Enter the purchase subtotal, such as 7.50.")
    dollars, _, cents = amount.partition(".")
    subtotal_cents = int(dollars) * 100 + int(cents.ljust(2, "0"))
    points = (subtotal_cents // 100) * CFG["points_per_dollar"]
    if points < 1:
        raise ApiError(400, "The subtotal must earn at least one point.")
    with LOCK, db() as con:
        member = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        if not member:
            raise ApiError(404, "No rewards account found. Ask the customer to join first.")
        result = con.execute(
            "INSERT INTO receipt_credits VALUES (?, ?, ?, ?) ON CONFLICT(receipt) DO NOTHING",
            (receipt, phone, subtotal_cents, points),
        )
        already_credited = result.rowcount == 0
        if already_credited:
            saved = con.execute("SELECT * FROM receipt_credits WHERE receipt=?", (receipt,)).fetchone()
            if saved["phone"] != phone or saved["subtotal_cents"] != subtotal_cents:
                raise ApiError(409, "That receipt reference has already been used.")
            points = saved["points"]
        else:
            add_points(con, phone, points, f"In-store purchase · {receipt}")
            member = con.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
    return {"customer": public_customer(member), "points": points, "already_credited": already_credited}


def h_staff_orders(ctx):
    require_staff(ctx["headers"])
    with db() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE status IN (?,?,?) OR "
            "(status IN ('picked_up','cancelled') AND created >= ?) ORDER BY pickup_time, id",
            (*ACTIVE, (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"))).fetchall()
        return {"orders": [serialize_order(r, staff=True) for r in rows],
                "paused": get_setting(con, "paused", "0") == "1",
                "ordering_enabled": CFG["ordering_enabled"]}


def h_staff_status(ctx, public_id):
    require_staff(ctx["headers"])
    new = ctx["body"].get("status")
    if new not in STATUSES:
        raise ApiError(400, "Invalid status.")
    with LOCK, db() as con:
        o = get_order(con, public_id)
        old = o["status"]
        if old in ("picked_up", "cancelled") and new != old:
            raise ApiError(409, f"Order is already {old.replace('_', ' ')}.")
        con.execute("UPDATE orders SET status=? WHERE id=?", (new, o["id"]))
        if new == "picked_up" and old != "picked_up" and o["phone"]:
            pts = (o["total_cents"] // 100) * CFG["points_per_dollar"]
            if pts:
                add_points(con, o["phone"], pts, f"Order #{o['public_id'][-6:]}", o["id"])
                con.execute("UPDATE orders SET points_awarded=? WHERE id=?", (pts, o["id"]))
        if new == "cancelled" and old != "cancelled" and o["phone"] and o["reward_cost"]:
            add_points(con, o["phone"], o["reward_cost"], f"Refund: order #{o['public_id'][-6:]} cancelled", o["id"])
        return serialize_order(get_order(con, public_id), staff=True)


def h_staff_pause(ctx):
    require_staff(ctx["headers"])
    if not CFG["ordering_enabled"]:
        raise ApiError(409, "Online ordering is on hold.")
    paused = bool(ctx["body"].get("paused"))
    with db() as con:
        set_setting(con, "paused", "1" if paused else "0")
    return {"paused": paused}


ROUTES = [
    ("GET", r"/api/config", h_config),
    ("GET", r"/api/menu", h_menu),
    ("GET", r"/api/slots", h_slots),
    ("POST", r"/api/signup", h_signup),
    ("POST", r"/api/login", h_login),
    ("POST", r"/api/logout", h_logout),
    ("GET", r"/api/me", h_me),
    ("POST", r"/api/quote", h_quote),
    ("POST", r"/api/orders", h_create_order),
    ("GET", r"/api/orders/([A-Za-z0-9]+)", h_get_order),
    ("POST", r"/api/orders/([A-Za-z0-9]+)/arrived", h_arrived),
    ("POST", r"/api/staff/loyalty/lookup", h_staff_member),
    ("POST", r"/api/staff/loyalty/credit", h_staff_credit),
    ("GET", r"/api/staff/orders", h_staff_orders),
    ("POST", r"/api/staff/orders/([A-Za-z0-9]+)/status", h_staff_status),
    ("POST", r"/api/staff/pause", h_staff_pause),
]

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json",
        ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_file(self, file, status=200, data=None):
        if data is None:
            data = file.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", MIME.get(file.suffix, "application/octet-stream") + ("; charset=utf-8" if file.suffix in (".html", ".css", ".js") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if file.name == "pilot.html" else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def pilot_access(self):
        if not PILOT_CODE:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        proof = cookie.get(PILOT_COOKIE)
        return proof is not None and hmac.compare_digest(proof.value.encode(), PILOT_PROOF.encode())

    def pilot_login(self, path):
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return self.send_json(415, {"error": "Use the pilot sign-in form."})
        fields = parse_qs(self.read_body().decode(), max_num_fields=4)
        code = fields.get("code", [""])[0].strip()
        if not PILOT_CODE or not hmac.compare_digest(code.encode(), PILOT_CODE.encode()):
            file = STATIC / "pilot.html"
            message = b'<p class="err" role="alert">Check the pilot access code and try again.</p>'
            return self.send_file(file, 403, file.read_bytes().replace(b"<!-- error -->", message))
        self.send_response(303)
        self.send_header("Location", path)
        secure = "; Secure" if ON_RENDER else ""
        self.send_header("Set-Cookie", f"{PILOT_COOKIE}={PILOT_PROOF}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800{secure}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid content length.")
        if not 0 <= size <= 64 * 1024:
            raise ApiError(413, "Request is too large.")
        return self.rfile.read(size)

    def serve_static(self, path):
        path = {"/": "/index.html", "/staff": "/staff.html"}.get(path, path)
        if path in ("/index.html", "/staff.html") and not self.pilot_access():
            return self.send_file(STATIC / "pilot.html")
        file = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in file.parents or not file.is_file():
            return self.send_json(404, {"error": "Not found."})
        return self.send_file(file)

    def dispatch(self, method):
        path = urlparse(self.path).path
        if method == "GET" and path == "/health":
            with db() as con:
                con.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
            return self.send_json(200, {"status": "ok"})
        if not path.startswith("/api/"):
            if method == "GET":
                return self.serve_static(path)
            if method == "POST" and path in ("/", "/staff", "/index.html", "/staff.html"):
                try:
                    return self.pilot_login(path)
                except ApiError as e:
                    return self.send_json(e.status, {"error": e.msg})
                except ValueError:
                    return self.send_json(400, {"error": "Bad request body."})
            return self.send_json(405, {"error": "Method not allowed."})
        if not self.pilot_access():
            return self.send_json(403, {"error": "Pilot access expired. Reload and enter the access code."})
        body = {}
        if method == "POST":
            if self.headers.get_content_type() != "application/json":
                return self.send_json(415, {"error": "Use an application/json request."})
            try:
                body = json.loads(self.read_body() or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ApiError as e:
                return self.send_json(e.status, {"error": e.msg})
            except ValueError:
                return self.send_json(400, {"error": "Bad request body."})
        for m, pat, fn in ROUTES:
            mt = re.fullmatch(pat, path)
            if m == method and mt:
                try:
                    return self.send_json(200, fn({"body": body, "headers": self.headers}, *mt.groups()))
                except ApiError as e:
                    return self.send_json(e.status, {"error": e.msg})
                except Exception as e:
                    print("[error]", repr(e))
                    return self.send_json(500, {"error": "Something went wrong on our end."})
        self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


if __name__ == "__main__":
    if ON_RENDER and (
        len(PILOT_CODE) < 16 or len(STAFF_PIN) < 16 or STAFF_PIN == PILOT_CODE
        or CLOVER.name != "mock"
    ):
        raise SystemExit("Render pilot requires separate strong access and staff codes and simulated Clover.")
    if ON_RENDER:
        db_root = "/tmp" if TEMPORARY_DB else "/data"
        if not Path(DB_PATH).resolve().is_relative_to(db_root):
            raise SystemExit(f"Set DB_PATH to a SQLite file under {db_root}.")
        if TEMPORARY_DB:
            print("Temporary pilot storage is enabled. Test data may reset when Render stops or redeploys.")
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    print(f"{CFG['shop_name']} running on http://localhost:{port}  (POS: {CLOVER.name})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
