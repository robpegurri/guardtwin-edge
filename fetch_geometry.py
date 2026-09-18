#!/usr/bin/env python3
r"""Auxiliary, offline tool: downloads static site geometry (buildings,
roads, amenities) around a point from OpenStreetMap via the Overpass API
and writes it as a GeoJSON FeatureCollection, in the shape broker.py's
--geometry-file expects.

Not run by broker.py itself, and not run automatically: the public
Overpass instance is unreliable enough (frequent 504s under load) that
fetching it live on every broker startup isn't worth it for geometry
that doesn't change at runtime. Run this by hand instead, once per site
(or whenever the survey needs updating), inspect the result, then point
--geometry-file at it.

Usage:
    python3 fetch_geometry.py --lat 45.064924 --lon 7.659707 --radius-m 300 \
        --out geometry.geojson

Only the Python standard library is used.
"""

import argparse
import json
import logging
import math
import sys
import time
import urllib.parse
import urllib.request

log = logging.getLogger("fetch-geometry")

RETRY_BACKOFF_S = 3.0


def _bbox_square(center_lat, center_lon, radius_m):
    """(south, west, north, east) bounds of a 2*radius_m-side square
    centered on (center_lat, center_lon), flat-earth approximation
    (adequate at the ~100-1000 m scale used here)."""
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * math.cos(math.radians(center_lat)))
    return (center_lat - dlat, center_lon - dlon,
            center_lat + dlat, center_lon + dlon)


def _overpass_query(south, west, north, east, timeout_s):
    bbox = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    return (
        f"[out:json][timeout:{int(timeout_s)}];"
        "("
        f'way["building"]({bbox});'
        f'way["highway"]({bbox});'
        f'node["amenity"]({bbox});'
        f'way["amenity"]({bbox});'
        ");"
        "out center;"
    )


def _overpass_to_geojson(elements):
    """Overpass JSON elements -> a GeoJSON FeatureCollection of Points
    (ways/relations use their `out center` centroid; OSM tags become
    GeoJSON properties) -- exactly the shape broker.py's --geometry-file
    expects."""
    features = []
    for el in elements:
        if el.get("type") == "node":
            lon, lat = el.get("lon"), el.get("lat")
        else:
            center = el.get("center") or {}
            lon, lat = center.get("lon"), center.get("lat")
        if lon is None or lat is None:
            continue
        features.append({
            "type": "Feature",
            "properties": el.get("tags") or {},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })
    return {"type": "FeatureCollection", "features": features}


def _download(query, overpass_url, http_timeout, max_bytes):
    """One attempt: POST the query, return the parsed JSON doc. Raises on
    any network/HTTP/size/parse problem -- the caller decides whether to
    retry."""
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        overpass_url, data=body, method="POST",
        headers={"User-Agent": "guardtwin-sensor-fusion/1.0"})
    with urllib.request.urlopen(req, timeout=http_timeout) as r:
        raw = r.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"response over --max-bytes ({max_bytes})")
    return json.loads(raw)


def fetch(lat, lon, radius_m, overpass_url, http_timeout, max_bytes, attempts):
    """Downloads OSM buildings/roads/amenities in a square of side
    2*radius_m centered on (lat, lon). Retries a few times (the public
    Overpass instance answers 504 fairly often under load, transiently --
    a later attempt, possibly routed to a different backend, often
    succeeds); raises RuntimeError if every attempt fails."""
    south, west, north, east = _bbox_square(lat, lon, radius_m)
    query = _overpass_query(south, west, north, east, http_timeout)
    log.info("downloading from %s (center=%.6f,%.6f radius=%.0fm, "
             "bbox=%.6f,%.6f,%.6f,%.6f)",
             overpass_url, lat, lon, radius_m, south, west, north, east)

    doc = None
    for attempt in range(1, attempts + 1):
        try:
            doc = _download(query, overpass_url, http_timeout, max_bytes)
            break
        except Exception as e:
            log.warning("attempt %d/%d failed (%s)", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(RETRY_BACKOFF_S)
    if doc is None:
        raise RuntimeError(f"Overpass unreachable after {attempts} attempt(s)")

    return _overpass_to_geojson(doc.get("elements") or [])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lat", type=float, required=True, help="center latitude")
    ap.add_argument("--lon", type=float, required=True, help="center longitude")
    ap.add_argument("--radius-m", type=float, default=300.0,
                    help="half-side, in meters, of the downloaded square "
                         "(total square side = 2x this)")
    ap.add_argument("--out", default="geometry.geojson",
                    help="output GeoJSON path")
    ap.add_argument("--overpass-url",
                    default="https://overpass-api.de/api/interpreter",
                    help="Overpass API endpoint; point this at a mirror or "
                         "self-hosted instance if the public one is down")
    ap.add_argument("--http-timeout", type=float, default=30.0,
                    help="HTTP timeout per attempt")
    ap.add_argument("--attempts", type=int, default=5,
                    help="download attempts before giving up (the public "
                         "server 504s fairly often under load, transiently)")
    ap.add_argument("--max-bytes", type=int, default=20_000_000,
                    help="size guard on the Overpass response")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        collection = fetch(args.lat, args.lon, args.radius_m,
                            args.overpass_url, args.http_timeout,
                            args.max_bytes, args.attempts)
    except RuntimeError as e:
        log.critical("%s -- try again later, or pass --overpass-url to use "
                    "a different instance", e)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(collection, f, indent=2)
    log.info("wrote %d feature(s) to %s", len(collection["features"]), args.out)


if __name__ == "__main__":
    main()
