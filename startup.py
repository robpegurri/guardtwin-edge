"""
    STARTUP ORCHESTRATOR WORKFLOW

    Phase 1 -- health-check of the ENVELOPE Devices Location APIs
    (envelope_location.py). The AoI subscription is escalator.py's, which
    uses its callbacks to track the devices in the AoI.

    Phase 2 -- wait for the sensing bike to appear: the bike is
    whoever sends the first radar UDP frame to broker.py's --listen-port;
    its source IP is logged as the sensing node found.

    Phase 3 -- supervision: as implemented in broker.py. This script
    launches it unchanged with whatever extra arguments were given on the
    command line.
"""

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys

import envelope_location as loc

log = logging.getLogger("startup")


def peek_listen_addr(broker_args):

    # Take --listen-host/--listen-port from args
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, default=30490)
    known, _ = p.parse_known_args(broker_args)
    return known.listen_host, known.listen_port


def wait_for_radar_frame(host, port, timeout_s):

    # Bind and wait for the first UDP packet to arrive, then return its source IP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    if timeout_s > 0:
        sock.settimeout(timeout_s)
    try:
        _, (ip, _) = sock.recvfrom(65535)
        log.info("Startup Phase 2 Completed: radar frame received from %s -- sensing node found!", ip)
        return ip
    except socket.timeout:
        return None
    finally:
        sock.close()


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

    log.info("Startup Phase 1: checking ENVELOPE Devices Location APIs health...")
    loc.wait_alive()

    # `docker stop` (also run by `docker compose up` when the settings change)
    # sends SIGTERM to this process: make it a normal exit, so that the
    # finally below stops the broker. Python's default would die on the spot.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if args.skip_device_detect:
        log.info("Startup Phase 2: skipped (--skip-device-detect)")
    else:
        listen_host, listen_port = peek_listen_addr(broker_args)
        log.info("Startup Phase 2: waiting for the first radar frame on udp://%s:%d",
                 listen_host, listen_port)
        node = wait_for_radar_frame(listen_host, listen_port, args.detect_timeout_s)
        if node is None:
            log.error("Startup Phase 2: no radar frame received within timeout")
            return 1

    broker_py = args.broker_py or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "broker.py")

    # broker.py gets the AoI too, so it can drop frames from outside it
    aoi_args = ["--aoi-lat", str(args.aoi_lat),
               "--aoi-lon", str(args.aoi_lon),
               "--aoi-radius", str(args.aoi_radius)]
    log.info("Startup Phase 3: starting supervision (%s)...", broker_py)
    broker = subprocess.Popen([sys.executable, broker_py, *aoi_args, *broker_args])
    try:
        return broker.wait()
    finally:
        if broker.poll() is None:            # we are being stopped
            broker.terminate()
            try:
                broker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                broker.kill()


if __name__ == "__main__":
    sys.exit(main())