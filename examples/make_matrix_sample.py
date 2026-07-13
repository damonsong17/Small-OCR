"""Generate a synthetic *wide matrix* quote sheet to test table_parser offline.

Mirrors the real-world structure: a category banner, currency group headers,
BID/OFFER column headers, multiple currencies per tenor row, a stacked second
table, and a missing cell rendered as '-'.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent


def _font(size: int):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/consola.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make(out: Path) -> None:
    img = Image.new("RGB", (1500, 560), "white")
    d = ImageDraw.Draw(img)
    f = _font(20)

    def cell(x, y, text):
        d.text((x, y), text, font=f, fill="black")

    # Column x-centres (left-aligned text, roughly column starts).
    tx = 20
    xs = [140, 260, 360, 480, 600, 700, 820, 960, 1060, 1200, 1340, 1440]

    # ---- Top block: category + currency groups ----
    cell(560, 12, "Chinese")
    for gx, name in zip([200, 540, 900, 1260], ["USD", "EUR", "CNH", "HKD"]):
        cell(gx, 44, name)

    headers = ["SOFR", "BID", "OFFER", "EURIBOR", "BID", "OFFER",
               "CNH HIBOR", "BID", "OFFER", "HKD HIBOR", "BID", "OFFER"]
    cell(tx, 78, "Tenor")
    for x, h in zip(xs, headers):
        cell(x, 78, h)

    top_rows = [
        ("o/n", ["3.53000", "3.60", "3.65", "2.182", "2.16", "2.30",
                 "1.42848", "1.30", "1.50", "2.64119", "-", "-"]),  # HKD bid/offer missing
        ("1w", ["3.62280", "3.70", "3.72", "2.168", "2.20", "2.30",
                "1.45000", "1.15", "1.45", "2.60000", "2.35", "2.65"]),
        ("2w", ["3.62770", "3.72", "3.76", "2.190", "2.25", "2.35",
                "1.47303", "1.20", "1.50", "2.60637", "2.45", "2.60"]),
        ("1s", ["3.65942", "3.80", "3.85", "2.270", "2.30", "2.50",
                "1.53879", "1.30", "1.55", "2.66042", "2.50", "2.75"]),
        ("6s", ["3.85968", "4.13", "4.18", "2.626", "2.50", "2.75",
                "1.68424", "1.35", "1.60", "3.17964", "2.95", "3.20"]),
        ("1y", ["4.01971", "4.24", "4.45", "2.831", "2.70", "2.85",
                "1.71939", "1.30", "1.65", "3.54958", "3.10", "3.45"]),
    ]
    y = 110
    for tenor, vals in top_rows:
        cell(tx, y, tenor)
        for x, v in zip(xs, vals):
            cell(x, y, v)
        y += 30

    # ---- Bottom block: market groups, USD/EUR bid-offer pairs ----
    y += 24
    for gx, name in zip([180, 440, 720, 1000], ["Korean", "Taiwanese", "Indian", "ISLAMIC"]):
        cell(gx, y, name)
    y += 30
    bxs = [140, 300, 460, 620, 780, 940, 1100, 1260]
    bheaders = ["USD BID", "USD OFFER", "USD BID", "USD OFFER",
                "USD BID", "USD OFFER", "EUR BID", "USD OFFER"]
    cell(tx, y, "Tenor")
    for x, h in zip(bxs, bheaders):
        cell(x, y, h)
    y += 30
    bottom_rows = [
        ("o.n", ["3.63", "3.65", "3.60", "3.67", "3.63", "3.67", "2.25", "3.65"]),
        ("1w", ["3.65", "3.72", "3.65", "3.72", "3.90", "4.00", "2.30", "3.75"]),
        ("1y", ["4.35", "4.45", "4.25", "4.38", "4.70", "4.90", "2.80", "4.70"]),
    ]
    for tenor, vals in bottom_rows:
        cell(tx, y, tenor)
        for x, v in zip(bxs, vals):
            cell(x, y, v)
        y += 30

    img.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    make(HERE / "sample_matrix_quote.png")
