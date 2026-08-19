import os
import sys
import smtplib
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)

POULTRY_BASE = "https://lahore.punjab.gov.pk"
POULTRY_PAGE = f"{POULTRY_BASE}/poultry-rate-list"
PETROLEUM_PAGE = "https://petroleum.gov.pk/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_image(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    return resp.content


def get_poultry(session: requests.Session) -> tuple[str, str, bytes]:
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
    return image_url, date_str, fetch_image(session, image_url)


def get_petroleum(session: requests.Session) -> tuple[str, str, bytes]:
    """Returns (detail_url, title, image_bytes)."""
    resp = session.get(PETROLEUM_PAGE, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # First article → first paragraph link (most recent notification)
    articles = soup.select("main section div div article")
    if not articles:
        raise RuntimeError("No articles found on petroleum.gov.pk.")
    link = articles[0].select_one("div p:first-of-type a")
    if not link:
        raise RuntimeError("No link in first petroleum article.")

    detail_url = link["href"]
    if not detail_url.startswith("http"):
        detail_url = "https://petroleum.gov.pk" + detail_url
    title = link.get_text(strip=True)

    # Follow link → find the notification image
    detail_resp = session.get(detail_url, timeout=20)
    detail_resp.raise_for_status()
    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")

    img = next(
        (i for i in detail_soup.find_all("img")
         if i.get("src") and "logo" not in i["src"].lower() and "icon" not in i["src"].lower()),
        None,
    )
    if not img:
        raise RuntimeError("No notification image found on petroleum detail page.")

    img_url = img["src"]
    if not img_url.startswith("http"):
        img_url = "https://petroleum.gov.pk" + img_url

    return detail_url, title, fetch_image(session, img_url)


def send_email(subject: str, html_body: str,
               poultry_img: bytes | None, petrol_img: bytes | None) -> None:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    for cid, data in [("poultry_slip", poultry_img), ("petrol_slip", petrol_img)]:
        if data:
            img = MIMEImage(data)
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
            msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())


def build_html(date_str: str, poultry_ok: bool, petrol_ok: bool,
               petrol_title: str, petrol_url: str) -> str:
    def img_block(cid: str, alt: str, ok: bool) -> str:
        if ok:
            return f"<img src='cid:{cid}' alt='{alt}' style='width:100%;border:1px solid #ddd;border-radius:4px;'>"
        return f"<p style='color:#c00;'>Image could not be fetched.</p>"

    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#222;">
  <h2 style="background:#1a6b3c;color:#fff;padding:12px 18px;border-radius:6px;margin:0 0 20px;">
    Daily Rates &ndash; {date_str}
  </h2>

  <h3 style="margin:0 0 8px;">&#x1F413; Lahore Poultry Rates</h3>
  <div style="margin-bottom:6px;">{img_block("poultry_slip", f"Poultry Rate Slip {date_str}", poultry_ok)}</div>
  <p style="font-size:12px;color:#888;margin:0 0 24px;">
    Source: <a href="{POULTRY_PAGE}" style="color:#1a6b3c;">Lahore DC Office &ndash; Poultry Rate List</a>
  </p>

  <h3 style="margin:0 0 8px;">&#x26FD; Petroleum Prices</h3>
  <div style="margin-bottom:6px;">{img_block("petrol_slip", petrol_title, petrol_ok)}</div>
  <p style="font-size:12px;color:#888;margin:0;">
    Source: <a href="{petrol_url}" style="color:#1a6b3c;">Ministry of Energy (Petroleum Division)</a>
  </p>

  <hr style="border:none;border-top:0.5px solid #eee;margin:20px 0 12px;">
  <p style="font-size:11px;color:#bbb;margin:0;">Sent automatically via GitHub Actions at 9:00 AM PKT.</p>
</body></html>
"""


def main():
    session = make_session()
    errors = []

    poultry_img = petrol_img = None
    date_str = "Today"
    petrol_title = "Petroleum Prices Notification"
    petrol_url = PETROLEUM_PAGE

    try:
        _, date_str, poultry_img = get_poultry(session)
        print(f"Poultry image fetched ({len(poultry_img)} bytes)")
    except Exception as exc:
        errors.append(f"Poultry: {exc}")
        print(f"ERROR poultry: {exc}", file=sys.stderr)

    try:
        petrol_url, petrol_title, petrol_img = get_petroleum(session)
        print(f"Petroleum image fetched ({len(petrol_img)} bytes) — {petrol_title}")
    except Exception as exc:
        errors.append(f"Petroleum: {exc}")
        print(f"ERROR petroleum: {exc}", file=sys.stderr)

    html = build_html(date_str, poultry_img is not None, petrol_img is not None,
                      petrol_title, petrol_url)
    subject = f"Daily Rates – {date_str}" + (" ⚠️ (partial)" if errors else "")

    try:
        send_email(subject, html, poultry_img, petrol_img)
        print(f"Email sent to {RECIPIENT}.")
    except Exception as exc:
        print(f"ERROR sending email: {exc}", file=sys.stderr)
        sys.exit(1)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
