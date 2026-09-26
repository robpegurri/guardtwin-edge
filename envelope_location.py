"""
    ENVELOPE LOCATION APIs - Devices-in-Area
"""

import json
import logging
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue, Empty

import requests

import recorder

log = logging.getLogger("envelope_location")

BASE = "http://192.168.0.38:3113/devices-in-area/v1"
USER_ID = "325F2093-1AA0-40AE-B502-821906574E00"
HEADERS = {"X-Camara-User-Id": USER_ID, "Content-Type": "application/json"}


def area(lat, lon, radius=150):
    return {"areaType": "CIRCLE",
            "center": {"latitude": lat, "longitude": lon},
            "radius": radius}


# 0. health ----------------------------------------------------------------

def is_alive():
    """True if the service and its upstream location_api are both up."""
    try:
        doc = requests.get(f"{BASE}/health", timeout=5).json()
    except Exception as e:
        log.debug("GET %s/health failed: %s", BASE, e)
        return False
    log.debug("GET %s/health -> %s", BASE, doc)
    return doc.get("status") == "up" and \
        doc.get("checks", {}).get("location_api", {}).get("status") == "up"


def wait_alive(timeout=60):
    """Poll until healthy. The upstream flaps, so retry instead of failing."""
    log.info("waiting for %s to become healthy (timeout %ss)", BASE, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_alive():
            log.info("%s is healthy", BASE)
            return
        log.debug("not healthy yet, retrying in 3s")
        time.sleep(3)
    raise TimeoutError("service not healthy")


# 1. subscription ----------------------------------------------------------

def subscribe(a, sink, initial_event=True):
    """
    Create a subscription; returns its id.
    """
    body = {"sink": sink, "protocol": "HTTP",
            "config": {"initialEvent": initial_event,
                       "subscriptionDetail": {"area": a}}}
    log.debug("POST %s/subscriptions body=%s", BASE, body)
    r = requests.post(f"{BASE}/subscriptions", headers=HEADERS,
                      json=body, timeout=10)
    recorder.record("envelope", call="subscribe", request=body, status=r.status_code,
                    body=r.text)
    r.raise_for_status()
    sub_id = r.json()["id"]
    log.info("subscribed %s -> %s (area: %s)", sub_id, sink, a)
    return sub_id


def unsubscribe(sub_id):
    log.debug("DELETE %s/subscriptions/%s", BASE, sub_id)
    r = requests.delete(f"{BASE}/subscriptions/{sub_id}", headers=HEADERS, timeout=10)
    recorder.record("envelope", call="unsubscribe", id=sub_id, status=r.status_code,
                    body=r.text)
    r.raise_for_status()
    log.info("unsubscribed %s", sub_id)


def subscriptions():
    """All the subscriptions of this user id."""
    r = requests.get(f"{BASE}/subscriptions", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def our_subscriptions(base):
    """
    The subscriptions notifying this host's callback receiver: its sink is
    `base` (older runs) or `base`/<token> (see start_receiver). The base is
    this host's own callback URL, so they can only be ours.
    """
    return [sub for sub in subscriptions()
            if sub.get("sink") == base or str(sub.get("sink", "")).startswith(base + "/")]


# 2. callbacks -------------------------------------------------------------

def start_receiver(on_event, port=8080, advertise_host=None, on_stale=None):
    """
    Listen for the service's callbacks. Returns (sink, base, stop): subscribe
    with `sink`; `base` is what every sink of this host starts with.

    Each receiver gets a sink of its own, `base`/<random token>, and hands
    on_event(event) only the callbacks addressed to it. Callbacks for any
    other path come from older subscriptions that could not be deleted:
    acknowledged (so the service does not retry them) and dropped, with
    on_stale(path).

    `advertise_host` is the host ENVELOPE should call back on. Leave it
    unset only when running directly on the host that can reach
    ENVELOPE (it's then auto-detected from routing). When running in a
    container behind published ports, auto-detection would report the
    container's internal bridge IP -- unreachable from ENVELOPE -- so
    the externally-reachable host/IP must be passed explicitly.
    """
    path = "/notify/" + secrets.token_hex(8)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            self.send_response(204)          # ack first, handle after
            self.end_headers()
            recorder.record("callback", path=self.path, current=self.path == path,
                            src=self.client_address[0],
                            body=raw.decode("utf-8", "replace"))
            if self.path != path:
                log.debug("callback to %s from %s: not the current subscription, dropped",
                          self.path, self.client_address[0])
                if on_stale:
                    on_stale(self.path)
                return
            try:
                event = json.loads(raw)
            except ValueError:
                event = {"_raw": raw.decode("utf-8", "replace")}
            log.debug("notification from %s: %s", self.client_address[0], event)
            on_event(event)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if advertise_host:
        ip = advertise_host
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.38", 9))       # route lookup, sends nothing
        ip = s.getsockname()[0]
        s.close()

    base = f"http://{ip}:{port}/notify"
    log.info("listening for ENVELOPE callbacks on 0.0.0.0:%d (sink: %s%s)",
             port, base, path[len("/notify"):])
    return base + path[len("/notify"):], base, httpd.shutdown


# 3. query -----------------------------------------------------------------

def hostnames_in(a, max_age=60):
    """
    Get the hostnames of devices currently in the specified area.
    """
    body = {"area": a, "maxAge": max_age}
    log.debug("POST %s/queries body=%s", BASE, body)
    r = requests.post(f"{BASE}/queries", headers=HEADERS,
                      json=body, timeout=10)
    r.raise_for_status()
    hosts = sorted(d["hostname"] for d in r.json())
    log.debug("POST %s/queries -> %d device(s): %s", BASE, len(hosts), hosts)
    return hosts


def ipv4_addresses_in(a, max_age=60):
    """
    Get the IPv4 addresses of devices currently in the specified area.
    """
    body = {"area": a, "maxAge": max_age}
    log.debug("POST %s/queries body=%s", BASE, body)
    try:
        r = requests.post(f"{BASE}/queries", headers=HEADERS,
                          json=body, timeout=10)
    except Exception as e:
        recorder.record("envelope", call="query", request=body, error=str(e))
        raise
    recorder.record("envelope", call="query", request=body, status=r.status_code,
                    body=r.text)
    r.raise_for_status()
    addresses = []
    for device in r.json():
        ipv4 = device.get("ipv4Address", {})
        address = {
            key: ipv4[key]
            for key in ("publicAddress", "privateAddress", "publicPort")
            if ipv4.get(key) is not None
        }
        addresses.append(address)
    log.debug("POST %s/queries -> %d device(s): %s",
              BASE, len(addresses), addresses)
    return addresses


# --------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    links = area(45.064924, 7.659707, 150)

    wait_alive()
    events = Queue()
    sink, _, stop = start_receiver(events.put)      # before subscribing
    sub = subscribe(links, sink)
    print(f"subscribed {sub} -> {sink}")

    try:
        try:
            print("event:", json.dumps(events.get(timeout=60), indent=2))
        except Empty:
            print("no notification in 60s")

        print("in area:", hostnames_in(links))
    finally:
        unsubscribe(sub)
        stop()