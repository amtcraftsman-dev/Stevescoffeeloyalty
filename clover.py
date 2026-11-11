"""Clover POS adapter.

The app talks to Clover only through `submit_order(order)`. With no credentials
set it uses MockClover (logs and returns a fake id) so everything runs locally.

To go live, set:
    CLOVER_MERCHANT_ID   your merchant id
    CLOVER_TOKEN         an API token with Orders write permission
    CLOVER_BASE          https://apisandbox.dev.clover.com (sandbox, default)
                         https://api.clover.com            (production)

NOTE: RealClover has NOT been tested against a Clover account yet. Verify the
endpoints and payloads against https://docs.clover.com in the sandbox first.
"""
import json
import os
import urllib.request


class MockClover:
    name = "mock"

    def submit_order(self, order):
        cid = "MOCK-" + order["public_id"]
        print(f"[clover:mock] order {order['public_id']} -> {cid} "
              f"({len(order['lines'])} lines, ${order['total_cents'] / 100:.2f})")
        return cid


class RealClover:
    name = "clover"

    def __init__(self, merchant_id, token, base):
        self.mid, self.token, self.base = merchant_id, token, base.rstrip("/")

    def _post(self, path, body):
        req = urllib.request.Request(
            f"{self.base}/v3/merchants/{self.mid}{path}",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def submit_order(self, order):
        note = f"Pickup {order['pickup_time']}"
        if order.get("pickup_type") == "curbside":
            note += f" | CURBSIDE: {order.get('vehicle', '')}"
        o = self._post("/orders", {
            "state": "open",
            "title": f"Online #{order['public_id']} - {order['name']}",
            "note": note,
        })
        oid = o["id"]
        for ln in order["lines"]:
            for _ in range(ln["qty"]):  # Clover line items are one unit each
                self._post(f"/orders/{oid}/line_items", {
                    "name": ln["name"],
                    "price": ln["unit_cents"],
                    "note": ", ".join(ln["desc"]),
                })
        if order["discount_cents"]:
            self._post(f"/orders/{oid}/discounts", {
                "name": "Loyalty reward",
                "amount": -order["discount_cents"],
            })
        return oid


def get_client():
    mid, token = os.environ.get("CLOVER_MERCHANT_ID"), os.environ.get("CLOVER_TOKEN")
    if mid and token:
        return RealClover(mid, token, os.environ.get("CLOVER_BASE", "https://apisandbox.dev.clover.com"))
    return MockClover()
