"""Generate synthetic quote-sheet images for testing the pipeline offline."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent


def _font(size: int):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_fx_sheet(out: Path) -> None:
    img = Image.new("RGB", (760, 520), "white")
    d = ImageDraw.Draw(img)
    title, body = _font(26), _font(22)

    d.text((24, 16), "ACME BROKERS LTD", font=title, fill="black")
    d.text((24, 52), "FX Forward Quotes    Date: 13-Jul-2026", font=body, fill="black")

    sections = {
        "USD/CNY": [("O/N", "6.1234", "6.1240"),
                    ("1W", "6.1255", "6.1268"),
                    ("1M", "6.1310", "6.1325"),
                    ("3M", "6.1560", "6.1580"),
                    ("1Y", "6.2100", "6.2140")],
        "EUR/USD": [("1M", "1.0842", "1.0846"),
                    ("3M", "1.0871", "1.0876"),
                    ("6M", "1.0905", "1.0912")],
    }
    y = 100
    for ccy, rows in sections.items():
        d.text((24, y), ccy, font=title, fill="black")
        y += 40
        d.text((60, y), "Tenor", font=body, fill="black")
        d.text((260, y), "Bid", font=body, fill="black")
        d.text((460, y), "Offer", font=body, fill="black")
        y += 34
        for tenor, bid, offer in rows:
            d.text((60, y), tenor, font=body, fill="black")
            d.text((260, y), bid, font=body, fill="black")
            d.text((460, y), offer, font=body, fill="black")
            y += 32
        y += 16

    img.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    make_fx_sheet(HERE / "sample_fx_quote.png")
