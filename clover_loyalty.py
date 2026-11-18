"""Read-only Clover sandbox integration for in-store loyalty."""
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

SANDBOX = "https://apisandbox.dev.clover.com"
OBJECT_ID = re.compile(r"[A-Za-z0-9]{1,64}")
HOOK_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
LOCATIONS = ("HAVEN", "CENTRAL_PARK")
LABELS = {"HAVEN": "Haven", "CENTRAL_PARK": "Central Park"}


class CloverError(Exception):
    pass


def elements(obj, key):
    collection = obj.get(key)
    if isinstance(collection, dict):
        collection = collection.get("elements")
    return collection if isinstance(collection, list) else []


class SandboxLoyalty:
    def __init__(self, oauth=None):
        self.oauth = oauth
        self.path_token = os.environ.get("CLOVER_LOYALTY_PATH_TOKEN", "")
        self.auth_code = os.environ.get("CLOVER_LOYALTY_AUTH_CODE", "")
        self.app_id = os.environ.get("CLOVER_LOYALTY_APP_ID", "")
        self.merchants = {}
        for location in LOCATIONS:
            merchant_id = os.environ.get(f"CLOVER_{location}_SANDBOX_ID", "")
            token = os.environ.get(f"CLOVER_{location}_SANDBOX_TOKEN", "")
            if not merchant_id and not token:
                continue
            if not OBJECT_ID.fullmatch(merchant_id) or (not token and oauth is None) or merchant_id in self.merchants:
                raise ValueError(f"Set a distinct sandbox merchant ID and token for {location}.")
            self.merchants[merchant_id] = (location, token)
        if not HOOK_TOKEN.fullmatch(self.path_token) or not OBJECT_ID.fullmatch(self.app_id):
            raise ValueError("Set a random webhook path token (32+ URL-safe characters) and the sandbox app ID.")
        if not self.merchants:
            raise ValueError("Configure at least one Clover sandbox merchant.")

    def matches_path(self, token):
        return hmac.compare_digest(token.encode(), self.path_token.encode())

    def authenticated(self, code, app_id):
        return bool(self.auth_code and app_id == self.app_id
                    and hmac.compare_digest(code.encode(), self.auth_code.encode()))

    def get(self, merchant, resource, object_id, expand=""):
        query = "?" + urllib.parse.urlencode({"expand": expand}) if expand else ""
        token = self.merchants[merchant][1]
        if not token and self.oauth:
            from clover_oauth import OAuthError
            try:
                token = self.oauth.access_token(merchant)
            except OAuthError:
                raise CloverError("Reconnect the sandbox merchant at /clover/connect.") from None
        req = urllib.request.Request(
            f"{SANDBOX}/v3/merchants/{merchant}/{resource}/{object_id}{query}",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json", "User-Agent": "StevesCoffeeRewards/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise CloverError(f"Clover returned HTTP {error.code} for {resource}.") from None
        except (OSError, ValueError):
            raise CloverError(f"Could not read Clover {resource} data.") from None

    def enqueue(self, db, payload):
        merchants = payload.get("merchants")
        if not isinstance(merchants, dict):
            raise ValueError("Invalid Clover event body.")
        events = []
        for merchant, updates in merchants.items():
            if merchant not in self.merchants or not isinstance(updates, list):
                continue
            for update in updates:
                if not isinstance(update, dict):
                    continue
                kind, _, object_id = str(update.get("objectId", "")).partition(":")
                timestamp = update.get("ts")
                if kind in ("O", "P") and OBJECT_ID.fullmatch(object_id) and type(timestamp) is int:
                    events.append((merchant, kind, object_id, timestamp))
        with closing(db()) as conn, conn:
            conn.executemany(
                """INSERT OR IGNORE INTO clover_loyalty_events
                   (merchant, kind, object_id, event_time) VALUES (?,?,?,?)""", events
            )

    def process(self, db, normalize, add_points, cents_per_point, event):
        merchant, kind, object_id = event["merchant"], event["kind"], event["object_id"]
        order_id = object_id
        if kind == "P":
            payment = self.get(merchant, "payments", object_id)
            order_id = (payment.get("order") or {}).get("id") or ""
        if not isinstance(order_id, str) or not OBJECT_ID.fullmatch(order_id):
            return
        order = self.get(merchant, "orders", order_id, "customers,payments,refunds")
        if order.get("id") != order_id:
            raise CloverError("Clover returned a mismatched order.")
        state = order.get("paymentState")
        label = LABELS[self.merchants[merchant][0]]
        if state in ("REFUNDED", "CREDITED"):
            with closing(db()) as conn, conn:
                credit = conn.execute(
                    "SELECT member, points FROM clover_loyalty_credits WHERE merchant=? AND order_id=?",
                    (merchant, order_id),
                ).fetchone()
                if credit and credit["points"]:
                    add_points(conn, credit["member"], -credit["points"], f"Clover refund · {label} · {order_id}")
                    conn.execute(
                        "UPDATE clover_loyalty_credits SET points=0 WHERE merchant=? AND order_id=?",
                        (merchant, order_id),
                    )
                elif not credit:
                    # A late payment event cannot award a fully refunded order.
                    conn.execute("INSERT INTO clover_loyalty_credits VALUES (?,?,NULL,0)", (merchant, order_id))
            return
        if state == "PARTIALLY_REFUNDED" or elements(order, "refunds"):
            raise CloverError("Order has a refund that needs manual review.")
        if state != "PAID":
            if kind == "P" and payment.get("result") == "SUCCESS" and not payment.get("voided"):
                if state in ("OPEN", "PARTIALLY_PAID"):
                    raise CloverError("Payment arrived before the order was marked fully paid.")
            return
        payments = elements(order, "payments")
        if not payments:
            raise CloverError("Paid order is missing payment detail.")
        # Expanded order payments may omit fields supplied by the payment endpoint.
        complete_payments = []
        for summary in payments:
            if type(summary.get("taxAmount")) is not int:
                payment_id = summary.get("id", "")
                if not isinstance(payment_id, str) or not OBJECT_ID.fullmatch(payment_id):
                    raise CloverError("Paid order is missing a valid payment identifier.")
                detail = self.get(merchant, "payments", payment_id, "refunds")
                if detail.get("id") != payment_id:
                    raise CloverError("Clover returned a mismatched payment.")
                summary = {**summary, **detail}
                # Clover omits the zero tax field for no-tax payments on the
                # full payment endpoint. An explicit null/invalid tax still fails.
                # Do not expand taxRates: that requires unrelated inventory access.
                if "taxAmount" not in detail:
                    summary["taxAmount"] = 0
            complete_payments.append(summary)
        successful = [payment for payment in complete_payments
                      if payment.get("result") == "SUCCESS" and not payment.get("voided")]
        if not successful or any(elements(payment, "refunds") for payment in successful):
            raise CloverError("Paid order has no eligible payment or has a payment refund.")
        total = order.get("total")
        taxes = [payment.get("taxAmount", 0 if order.get("taxRemoved") is True else None)
                 for payment in successful]
        if type(total) is not int or total < 0 or any(type(tax) is not int or tax < 0 for tax in taxes):
            raise CloverError("Verify the total and tax on the Clover sandbox receipt.")
        points = max(0, total - sum(taxes)) // cents_per_point
        if not points:
            return
        customers = elements(order, "customers")
        if len(customers) != 1:
            return
        customer_id = customers[0].get("id") or ""
        if not isinstance(customer_id, str) or not OBJECT_ID.fullmatch(customer_id):
            return
        customer = self.get(merchant, "customers", customer_id, "emailAddresses,phoneNumbers")
        if customer.get("id") != customer_id:
            raise CloverError("Clover returned a mismatched customer.")
        candidates = {identifier for field, key in (("emailAddresses", "emailAddress"), ("phoneNumbers", "phoneNumber"))
                      for item in elements(customer, field) if (identifier := normalize(item.get(key)))}
        with closing(db()) as conn, conn:
            matched = [identifier for identifier in candidates if conn.execute(
                "SELECT 1 FROM customers WHERE phone=?", (identifier,)
            ).fetchone()]
            if len(matched) != 1:
                return
            member = matched[0]
            result = conn.execute(
                "INSERT OR IGNORE INTO clover_loyalty_credits VALUES (?,?,?,?)",
                (merchant, order_id, member, points),
            )
            if result.rowcount:
                add_points(conn, member, points, f"Clover purchase · {label} · {order_id}")

    def run(self, db, normalize, add_points, cents_per_point):
        while True:
            try:
                with closing(db()) as conn:
                    event = conn.execute(
                        """SELECT * FROM clover_loyalty_events WHERE attempts < 8 AND retry_at <= ?
                           ORDER BY retry_at, id LIMIT 1""", (int(time.time()),)
                    ).fetchone()
                if event is None:
                    time.sleep(2)
                    continue
                try:
                    self.process(db, normalize, add_points, cents_per_point, event)
                except CloverError as error:
                    attempts = event["attempts"] + 1
                    with closing(db()) as conn, conn:
                        conn.execute(
                            "UPDATE clover_loyalty_events SET attempts=?, retry_at=? WHERE id=?",
                            (attempts, int(time.time()) + min(10 * 2 ** (attempts - 1), 600), event["id"]),
                        )
                    print(f"[Clover sandbox] {event['kind']}:{event['object_id']}: {error}")
                else:
                    with closing(db()) as conn, conn:
                        conn.execute("DELETE FROM clover_loyalty_events WHERE id=?", (event["id"],))
            except Exception as error:
                print(f"[Clover sandbox] Worker error: {type(error).__name__}")
                time.sleep(5)
