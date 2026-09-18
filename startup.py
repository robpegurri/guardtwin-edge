#!/usr/bin/env python3
"""
Startup workflow orchestrator.

Phase 1 -- area of interest (AoI): health-check the ENVELOPE Devices
Location APIs (envelope_location.py) and subscribe to it.

Phase 2 -- wait for the sensing bike to enter the AoI: the bike is
whoever sends the first radar UDP frame to broker.py's --listen-port;
its source IP is logged as the sensing node found, no separate
identification protocol needed. Bypassable with --skip-device-detect
to jump straight to phase 3.

Phase 3 -- supervision: already implemented in broker.py. This script
launches it unchanged with whatever extra arguments were given on the
command line.
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading

import envelope_location as loc

log = logging.getLogger("startup")


# --------------------------------------------------------------------------
# Phase 2 -- first radar frame
# --------------------------------------------------------------------------

def peek_listen_addr(broker_args):
    """Pull --listen-host/--listen-port out of the args meant for
    broker.py, without duplicating its full parser (same defaults)."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, default=30490)
    known, _ = p.parse_known_args(broker_args)
    return known.listen_host, known.listen_port


def wait_for_radar_frame(host, port, timeout_s):
    """
    Bind broker.py's own UDP ingest port ourselves and wait for the
    first datagram; its source IP is the sensing node. The socket is
    closed right after (whether or not one arrived) so broker.py can
    bind the same port for phase 3 -- that first datagram is consumed
    here and won't reach broker.py, and any frame sent in the short gap
    between this closing and broker.py binding is lost, same as any
    other UDP packet broker.py isn't up yet to receive.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    if timeout_s > 0:
        sock.settimeout(timeout_s)
    try:
        _, (ip, _) = sock.recvfrom(65535)
        log.info("phase 2: radar frame received from %s -- sensing node found", ip)
        return ip
    except socket.timeout:
        return None
    finally:
        sock.close()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--aoi-lat", type=float, required=True,
                    help="area of interest center latitude")
    ap.add_argument("--aoi-lon", type=float, required=True,
                    help="area of interest center longitude")
    ap.add_argument("--aoi-radius", type=float, default=150,
                    help="area of interest radius in meters")
    ap.add_argument("--notify-port", type=int, default=8080,
                    help="local port for the ENVELOPE subscription callback")
    ap.add_argument("--notify-host", default=None,
                    help="externally-reachable host/IP ENVELOPE should call "
                         "back on; required when running in a container "
                         "behind published ports (auto-detected otherwise)")
    ap.add_argument("--skip-device-detect", action="store_true",
                    help="bypass phase 2 and go straight to phase 3 (supervision)")
    ap.add_argument("--detect-timeout-s", type=float, default=0,
                    help="phase 2: give up waiting for a radar frame after "
                         "this many seconds (0 = wait indefinitely)")
    ap.add_argument("--broker-py", default=None,
                    help="path to broker.py (default: next to this script)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args, broker_args = ap.parse_known_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if not broker_args:
        ap.error("no broker.py arguments given (e.g. --imsi ...); "
                 "unrecognized arguments are forwarded to it as-is")

    area = loc.area(args.aoi_lat, args.aoi_lon, args.aoi_radius)

    log.info("phase 1: checking ENVELOPE Devices Location APIs health")
    loc.wait_alive()

    events, sink, stop = loc.start_receiver(args.notify_port, args.notify_host)
    sub = loc.subscribe(area, sink)

    # Notifications are logged as they arrive (envelope_location.py's
    # receiver); this just drains the queue so it doesn't grow unbounded
    # for as long as phase 3 runs. Daemon thread: dies with the process.
    def drain_events():
        while True:
            events.get()
    threading.Thread(target=drain_events, daemon=True).start()

    try:
        if args.skip_device_detect:
            log.info("phase 2: skipped (--skip-device-detect)")
        else:
            listen_host, listen_port = peek_listen_addr(broker_args)
            log.info("phase 2: waiting for the first radar frame on udp://%s:%d",
                     listen_host, listen_port)
            node = wait_for_radar_frame(listen_host, listen_port, args.detect_timeout_s)
            if node is None:
                log.error("phase 2: no radar frame received within timeout")
                return 1

        broker_py = args.broker_py or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "broker.py")
        # broker.py gets the same AoI as the phase 1 subscription, so it can
        # drop frames from outside it -- one AoI definition, not two.
        aoi_args = ["--aoi-lat", str(args.aoi_lat),
                   "--aoi-lon", str(args.aoi_lon),
                   "--aoi-radius", str(args.aoi_radius)]
        log.info("phase 3: starting supervision (%s)", broker_py)
        result = subprocess.run([sys.executable, broker_py, *aoi_args, *broker_args])
        return result.returncode
    finally:
        loc.unsubscribe(sub)
        stop()


if __name__ == "__main__":
    sys.exit(main())
