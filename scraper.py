import os
import re
import sys
import time
import smtplib
import requests
import cloudscraper
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import pdfplumber
from io import BytesIO

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)

POULTRY_BASE = "https://lahore.punjab.gov.pk"
POULTRY_PAGE = f"{POULTRY_BASE}/poultry-rate-list"
OGRA_PRICES_PAGE = "https://ogra.org.pk/price-publications"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FUEL_LABELS = {
    "motor spirit": "Petrol (MS)",
    "ms ": "Petrol (MS)",
    "high speed diesel": "High Speed Diesel (HSD)",
    "hsd": "High Speed Diesel (HSD)",
    "light diesel": "Light Diesel Oil (LDO)",
    "ldo": "Light Diesel Oil (LDO)",
    "kerosene": "Kerosene Oil",
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def send_email(subject: str, html_body: str, inline_image: bytes | None = None) -> None:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    if inline_image:
        img = MIMEImage(inline_image)
        img.add_header("Content-ID", "<poultry_slip>")
        img.add_header("Content-Disposition", "inline", filename="poultry_rates.jpg")
        msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())


def get_latest_poultry(session: requests.Session) -> tuple[str, str, bytes]:
    """Returns (image_url, date_str, image_bytes)."""
    resp = session.get(POULTRY_PAGE, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    link = soup.select_one("table a")
    if not link:
        raise RuntimeError("No rate-slip link found on poultry page.")
    href = link["href"]
    image_url = href if href.startswith("http") else POULTRY_BASE + href
    date_cell = soup.select_one("table td:first-child")
    date_str = date_cell.get_text(strip=True) if date_cell else "Today"

    img_resp = session.get(image_url, timeout=20)
    img_resp.raise_for_status()
    return image_url, date_str, img_resp.content


def get_latest_fuel_pdf_url() -> str:
    """Uses cloudscraper to bypass Cloudflare and get the latest OGRA PDF link."""
    scraper = cloudscraper.create_scraper()
    scraper.get("https://ogra.org.pk/", timeout=15)  # warm up session/cookies
    time.sleep(1)
    resp = scraper.get(
        OGRA_PRICES_PAGE,
        timeout=20,
        headers={**HEADERS, "Referer": "https://ogra.org.pk/"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    link = soup.select_one("a.download-pdf[href]")
    if not link:
        raise RuntimeError("No PDF link found on OGRA page.")
    href = link["href"]
    pdf_url = href if href.startswith("http") else "https://ogra.org.pk" + href
    # Download the PDF via the same scraper (same session/cookies)
    pdf_resp = scraper.get(pdf_url, timeout=30)
    pdf_resp.raise_for_status()
    return pdf_url, pdf_resp.content


def parse_fuel_prices(pdf_bytes: bytes) -> list[tuple[str, str]]:
    prices = []
    seen = set()
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    for line in text.splitlines():
        line_lower = line.lower()
        for key, label in FUEL_LABELS.items():
            if key in line_lower and label not in seen:
                match = re.search(r"(\d{2,4}\.\d{2})", line)
                if match:
                    prices.append((label, match.group(1)))
                    seen.add(label)
                break
    return prices


def build_html(date_str: str, poultry_page: str, has_image: bool,
               fuel_prices: list, pdf_url: str) -> str:
    # Use CID reference if image was embedded, otherwise fall back to external URL
    img_src = "cid:poultry_slip" if has_image else ""

    fuel_rows = "".join(
        f"<tr{'style=\"background:#f9f9f9;\"' if i % 2 else ''}>"
        f"<td style='padding:8px 10px;border:0.5px solid #ddd;'>{name}</td>"
        f"<td style='padding:8px 10px;border:0.5px solid #ddd;text-align:right;'><b>Rs. {price}/L</b></td></tr>"
        for i, (name, price) in enumerate(fuel_prices)
    ) or "<tr><td colspan='2' style='padding:8px;color:#999;'>Could not extract prices from PDF.</td></tr>"

    chicken_block = (
        f"<img src='{img_src}' alt='Poultry Rate Slip {date_str}' style='width:100%;border:1px solid #ddd;border-radius:4px;'>"
        if has_image else
        "<p style='color:#c00;'>Poultry image could not be fetched.</p>"
    )

    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#222;">
  <h2 style="background:#1a6b3c;color:#fff;padding:12px 18px;border-radius:6px;margin:0 0 20px;">
    Daily Rates &ndash; {date_str}
  </h2>

  <h3 style="margin:0 0 8px;">&#x1F413; Lahore Poultry Rates</h3>
  <div style="margin-bottom:6px;">{chicken_block}</div>
  <p style="font-size:12px;color:#888;margin:0 0 24px;">
    Source: <a href="{poultry_page}" style="color:#1a6b3c;">Lahore DC Office &ndash; Poultry Rate List</a>
  </p>

  <h3 style="margin:0 0 8px;">&#x26FD; Petroleum Prices (OGRA)</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:6px;">
    <tr style="background:#f0f0f0;">
      <th style="padding:8px 10px;border:0.5px solid #ddd;text-align:left;">Fuel</th>
      <th style="padding:8px 10px;border:0.5px solid #ddd;text-align:right;">Price per Litre</th>
    </tr>
    {fuel_rows}
  </table>
  <p style="font-size:12px;color:#888;margin:0 0 0;">
    Source: <a href="{pdf_url}" style="color:#1a6b3c;">OGRA Daily Price Notification (PDF)</a>
    &nbsp;|&nbsp; Updated daily by OGRA.
  </p>

  <hr style="border:none;border-top:0.5px solid #eee;margin:20px 0 12px;">
  <p style="font-size:11px;color:#bbb;margin:0;">Sent automatically via GitHub Actions at 9:00 AM PKT.</p>
</body></html>
"""


def main():
    session = make_session()
    errors = []
    image_url = date_str = ""
    image_bytes = None
    fuel_prices = []
    pdf_url = OGRA_PRICES_PAGE

    try:
        image_url, date_str, image_bytes = get_latest_poultry(session)
        print(f"Poultry image fetched: {image_url} ({len(image_bytes)} bytes)")
    except Exception as exc:
        errors.append(f"Poultry: {exc}")
        print(f"ERROR poultry: {exc}", file=sys.stderr)

    try:
        pdf_url, pdf_bytes = get_latest_fuel_pdf_url()
        fuel_prices = parse_fuel_prices(pdf_bytes)
        print(f"Fuel prices: {fuel_prices}")
    except Exception as exc:
        errors.append(f"Fuel: {exc}")
        print(f"ERROR fuel: {exc}", file=sys.stderr)

    html = build_html(date_str or "Today", POULTRY_PAGE, image_bytes is not None,
                      fuel_prices, pdf_url)
    subject = f"Daily Rates – {date_str or 'Today'}" + (" ⚠️ (partial)" if errors else "")

    try:
        send_email(subject, html, inline_image=image_bytes)
        print(f"Email sent to {RECIPIENT}.")
    except Exception as exc:
        print(f"ERROR sending email: {exc}", file=sys.stderr)
        sys.exit(1)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
