import os
import re
import sys
import time
import smtplib
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
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


def send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())


def get_latest_poultry(session: requests.Session) -> tuple[str, str, str]:
    """Returns (image_url, date_str, slip_page_url)."""
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
    return image_url, date_str, POULTRY_PAGE


def get_latest_fuel_pdf_url(session: requests.Session) -> str:
    """Scrapes ogra.org.pk/price-publications and returns the first EN PDF link."""
    session.get("https://ogra.org.pk/", timeout=15)  # warm up cookies
    time.sleep(1)
    resp = session.get(OGRA_PRICES_PAGE, timeout=20,
                       headers={**HEADERS, "Referer": "https://ogra.org.pk/"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    link = soup.select_one("a.download-pdf[href]")
    if not link:
        raise RuntimeError("No PDF link found on OGRA price publications page.")
    href = link["href"]
    return href if href.startswith("http") else "https://ogra.org.pk" + href


def parse_fuel_prices_from_pdf(pdf_bytes: bytes) -> list[tuple[str, str]]:
    """Extract fuel name + price pairs from an OGRA PDF."""
    prices = []
    seen = set()
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    for line in text.splitlines():
        line_lower = line.lower()
        for key, label in FUEL_LABELS.items():
            if key in line_lower and label not in seen:
                # Find Rs. XX.XX or XXX.XX pattern
                match = re.search(r"(\d{2,4}\.\d{2})", line)
                if match:
                    prices.append((label, match.group(1)))
                    seen.add(label)
                break
    return prices


def build_email_html(image_url: str, date_str: str, poultry_page: str,
                     fuel_prices: list[tuple[str, str]], pdf_url: str) -> str:
    fuel_rows = "".join(
        f"<tr><td style='padding:8px 10px;border:0.5px solid #ddd;'>{name}</td>"
        f"<td style='padding:8px 10px;border:0.5px solid #ddd;text-align:right;'>"
        f"<b>Rs. {price}/L</b></td></tr>"
        for name, price in fuel_prices
    ) or "<tr><td colspan='2' style='padding:8px;color:#999;'>Could not extract prices from PDF.</td></tr>"

    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#222;">

  <h2 style="background:#1a6b3c;color:#fff;padding:12px 18px;border-radius:6px;margin:0 0 20px;">
    Daily Rates &ndash; {date_str}
  </h2>

  <!-- CHICKEN: full official rate slip image -->
  <h3 style="margin:0 0 8px;">🐔 Lahore Poultry Rates</h3>
  <a href="{image_url}" style="display:block;margin-bottom:6px;">
    <img src="{image_url}" alt="Poultry Rate Slip {date_str}"
         style="width:100%;border:1px solid #ddd;border-radius:4px;">
  </a>
  <p style="font-size:12px;color:#888;margin:0 0 24px;">
    Source: <a href="{poultry_page}" style="color:#1a6b3c;">Lahore DC Office – Poultry Rate List</a>
  </p>

  <!-- FUEL: parsed from OGRA PDF -->
  <h3 style="margin:0 0 8px;">⛽ Petroleum Prices (OGRA)</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:6px;">
    <tr style="background:#f0f0f0;">
      <th style="padding:8px 10px;border:0.5px solid #ddd;text-align:left;">Fuel</th>
      <th style="padding:8px 10px;border:0.5px solid #ddd;text-align:right;">Price per Litre</th>
    </tr>
    {fuel_rows}
  </table>
  <p style="font-size:12px;color:#888;margin:0 0 24px;">
    Source: <a href="{pdf_url}" style="color:#1a6b3c;">OGRA Daily Price Notification (PDF)</a>
    &nbsp;|&nbsp; Updated daily by OGRA.
  </p>

  <hr style="border:none;border-top:0.5px solid #eee;margin-bottom:12px;">
  <p style="font-size:11px;color:#bbb;margin:0;">Sent automatically via GitHub Actions at 9:00 AM PKT.</p>

</body></html>
"""


def main():
    session = make_session()
    errors = []

    image_url = date_str = poultry_page = ""
    fuel_prices = []
    pdf_url = OGRA_PRICES_PAGE

    try:
        image_url, date_str, poultry_page = get_latest_poultry(session)
        print(f"Poultry image: {image_url}")
    except Exception as exc:
        errors.append(f"Poultry: {exc}")
        print(f"ERROR poultry: {exc}", file=sys.stderr)

    try:
        pdf_url = get_latest_fuel_pdf_url(session)
        print(f"Fuel PDF: {pdf_url}")
        pdf_bytes = session.get(pdf_url, timeout=30, headers=HEADERS).content
        fuel_prices = parse_fuel_prices_from_pdf(pdf_bytes)
        print(f"Fuel prices: {fuel_prices}")
    except Exception as exc:
        errors.append(f"Fuel: {exc}")
        print(f"ERROR fuel: {exc}", file=sys.stderr)

    html = build_email_html(image_url, date_str or "Today", poultry_page, fuel_prices, pdf_url)
    subject = f"Daily Rates – {date_str or 'Today'}" + (" ⚠️ (partial)" if errors else "")

    try:
        send_email(subject, html)
        print(f"Email sent to {RECIPIENT}.")
    except Exception as exc:
        print(f"ERROR sending email: {exc}", file=sys.stderr)
        sys.exit(1)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
