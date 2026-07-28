"""Build the offline world-map assets from a Natural Earth countries GeoJSON.

Run this ONCE on a networked machine; the outputs are committed so the
air-gapped dashboard never needs a tile server or a CDN:

    python scripts/build_world_map.py ne_110m_admin_0_countries.geojson

Writes:
  treasury/static/world.geo.json      simplified country outlines (SVG paths)
  treasury/data/country_points.json   ISO-A2 -> {name, lat, lon} label anchors

Source: Natural Earth 1:110m Admin 0 Countries (public domain).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_GEO = os.path.join(HERE, "treasury", "static", "world.geo.json")
OUT_PTS = os.path.join(HERE, "treasury", "data", "country_points.json")


def _perp_distance(pt, start, end):
    (x, y), (x1, y1), (x2, y2) = pt, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def simplify(points, tol):
    """Ramer-Douglas-Peucker, iterative so deep rings don't blow the stack."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        worst, idx = 0.0, lo
        for i in range(lo + 1, hi):
            d = _perp_distance(points[i], points[lo], points[hi])
            if d > worst:
                worst, idx = d, i
        if worst > tol:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    return [p for p, k in zip(points, keep) if k]


def ring_area(ring):
    """Shoelace area in square degrees (sign ignored) — used to drop specks."""
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def ring_centroid(ring):
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if a == 0:
        return ring[0]
    a *= 0.5
    return [cx / (6 * a), cy / (6 * a)]


def clean(ring, tol, precision):
    ring = simplify(ring, tol)
    out, prev = [], None
    for x, y in ring:
        p = [round(x, precision), round(y, precision)]
        if p != prev:
            out.append(p)
        prev = p
    if len(out) < 4:
        return None
    if out[0] != out[-1]:
        out.append(list(out[0]))
    return out if len(out) >= 4 else None


def polygons_of(geom):
    """Normalise Polygon / MultiPolygon into a list of polygons (list of rings)."""
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def prop(props, *names, default=""):
    for n in names:
        v = props.get(n) or props.get(n.upper()) or props.get(n.lower())
        if v not in (None, "", -99, "-99"):
            return v
    return default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="ne_110m_admin_0_countries.geojson")
    ap.add_argument("--tolerance", type=float, default=0.30,
                    help="RDP tolerance in degrees (bigger = smaller file)")
    ap.add_argument("--min-area", type=float, default=0.30,
                    help="drop rings smaller than this many square degrees")
    ap.add_argument("--precision", type=int, default=2, help="decimal places kept")
    args = ap.parse_args(argv)

    with open(args.source, "r", encoding="utf-8") as fh:
        src = json.load(fh)

    features, points = [], {}
    for feat in src["features"]:
        props = feat.get("properties", {})
        name = prop(props, "NAME", "name", "ADMIN", "NAME_LONG")
        iso2 = prop(props, "ISO_A2_EH", "ISO_A2", "iso_a2")
        iso3 = prop(props, "ISO_A3_EH", "ISO_A3", "iso_a3")
        polys, biggest, biggest_area = [], None, 0.0
        for poly in polygons_of(feat.get("geometry")):
            rings = []
            for j, ring in enumerate(poly):
                area = ring_area(ring)
                if j == 0 and area < args.min_area:
                    break                      # whole island too small to draw
                simplified = clean(ring, args.tolerance, args.precision)
                if simplified is None:
                    if j == 0:
                        break
                    continue                   # a hole vanished; the shell stays
                rings.append(simplified)
                if j == 0 and area > biggest_area:
                    biggest_area, biggest = area, simplified
            if rings:
                polys.append(rings)
        if not polys:
            continue
        features.append({
            "type": "Feature",
            "properties": {"name": name, "iso2": iso2, "iso3": iso3},
            "geometry": {"type": "MultiPolygon", "coordinates": polys},
        })
        if iso2 and biggest:
            lon, lat = ring_centroid(biggest)
            points[iso2] = {
                "name": name, "iso3": iso3,
                "lat": round(lat, 2), "lon": round(lon, 2),
            }

    os.makedirs(os.path.dirname(OUT_GEO), exist_ok=True)
    os.makedirs(os.path.dirname(OUT_PTS), exist_ok=True)
    with open(OUT_GEO, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features},
                  fh, separators=(",", ":"))
    with open(OUT_PTS, "w", encoding="utf-8") as fh:
        json.dump(points, fh, separators=(",", ":"), sort_keys=True, indent=0)

    print(f"{len(features)} countries -> {OUT_GEO} "
          f"({os.path.getsize(OUT_GEO) / 1024:.0f} KB)")
    print(f"{len(points)} centroids -> {OUT_PTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
