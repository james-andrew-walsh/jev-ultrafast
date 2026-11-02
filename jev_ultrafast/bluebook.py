"""Collect company names, websites, and public emails from a Blue Book search. No model calls."""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .browser import Browser

FIELDS = ["company", "website", "emails", "email_sources", "profile_url", "status"]
MAX_SITE_PAGES = 3
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CONTACT_LINK = re.compile(r"contact|about|team|staff|people", re.I)
BOT_CHECK = re.compile(r"just a moment|attention required|are you (a )?human|verify you|access denied|captcha", re.I)
FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".pdf", ".zip", ".doc", ".docx")
LISTING_DOMAINS = ("thebluebook.com", "construction.com")
IGNORED_EMAIL_DOMAINS = (
    *LISTING_DOMAINS,
    "example.com",
    "domain.com",
    "email.com",
    "mailservice.com",
    "mysite.com",
    "yoursite.com",
    "yourdomain.com",
    "sentry.io",
    "wixpress.com",
)

READ_RESULTS = """(() => ({
  title: document.title,
  text: (document.body?.innerText || '').slice(0, 500),
  cards: [...document.querySelectorAll('.result-list .single_result_wrapper')].map(card => {
    const name = card.querySelector('a.company-name-btn');
    const links = [...card.querySelectorAll('a[href]')];
    return {
      company: name?.textContent.trim() || '',
      profile_url: name?.href || '',
      contact_url: links.find(a => a.textContent.trim() === 'Contact Us')?.href || '',
      website: links.find(a => a.textContent.trim() === 'Website')?.href || '',
    };
  }),
  next: [...document.querySelectorAll('a[href*="page="]')].find(a => /^next/i.test(a.textContent.trim()))?.href || '',
}))()"""

READ_PAGE = """(() => ({
  url: location.href,
  title: document.title,
  text: document.body?.innerText || '',
  mailto: [...document.querySelectorAll('a[href^="mailto:" i]')].map(a => a.getAttribute('href')),
  links: [...document.querySelectorAll('a[href]')].slice(0, 500)
    .map(a => ({href: a.href, text: a.textContent.trim().slice(0, 80)})),
}))()"""


class BotCheck(RuntimeError):
    """The site is asking whether this is a person. Stop; never try to get past it."""


def host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def is_listing(url):
    return any(host(url) == domain or host(url).endswith("." + domain) for domain in LISTING_DOMAINS)


def clean_website(url):
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def valid_email(email):
    domain = email.rsplit("@", 1)[-1]
    return not email.endswith(FILE_SUFFIXES) and not any(
        domain == ignored or domain.endswith("." + ignored) for ignored in IGNORED_EMAIL_DOMAINS
    )


def extract_emails(page):
    mailto = [
        address
        for href in page["mailto"]
        for address in unquote(href.split(":", 1)[1].split("?", 1)[0]).split(",")
    ]
    found = [m for text in [*mailto, page["text"]] for m in EMAIL.findall(text)]
    return [email for email in dict.fromkeys(e.lower() for e in found) if valid_email(email)]


def contact_links(page):
    """Same-site pages likely to list an address, with contact pages first."""
    base = page["url"]
    links = {}
    for link in page["links"]:
        url = urljoin(base, link["href"]).split("#", 1)[0]
        path = urlsplit(url).path.lower()
        if (
            url.rstrip("/") != base.split("#", 1)[0].rstrip("/")
            and host(url) == host(base)
            and not path.endswith(FILE_SUFFIXES)
            and CONTACT_LINK.search(f"{path} {link['text']}")
        ):
            links.setdefault(url, link["text"])
    return sorted(links, key=lambda url: "contact" not in f"{url} {links[url]}".lower())


def profile_website(page):
    return next(
        (link["href"] for link in page["links"] if link["text"] == "Website" and not is_listing(link["href"])),
        "",
    )


def allowed(url):
    """Apply each company site's own robots.txt as RFC 9309 does: 4xx means no rules; unreachable means stay out."""
    parts = urlsplit(url)
    try:
        response = httpx.get(f"{parts.scheme}://{parts.netloc}/robots.txt", follow_redirects=True, timeout=5)
    except httpx.HTTPError:
        return False
    if response.status_code >= 500:
        return False
    if response.is_error:
        return True
    rules = RobotFileParser()
    rules.parse(response.text.splitlines())
    return rules.can_fetch("*", url)


def check_bot(page):
    if BOT_CHECK.search(f"{page['title']} {page['text'][:500]}"):
        raise BotCheck(f"Bot check on {page.get('url', 'the results page')}: {page['title']}")


def navigate(browser, url):
    error = browser.goto(url)
    if error == "net::ERR_ABORTED":
        # The page can cancel a navigation with one of its own. Loading a page changes nothing, so try again once.
        time.sleep(1)
        error = browser.goto(url)
    if error:
        raise RuntimeError(error)


def read_page(browser, url):
    navigate(browser, url)
    return browser.evaluate(READ_PAGE)


def website_emails(browser, website, delay):
    """Return {email: page}, the pages visited, and any failure for one company's own site."""
    found, visited, queue = {}, [], [website]
    while queue and len(visited) < MAX_SITE_PAGES:
        url = queue.pop(0)
        if url in visited or not allowed(url):
            continue
        visited.append(url)
        time.sleep(delay)
        try:
            page = read_page(browser, url)
        except (RuntimeError, TimeoutError, ValueError) as error:
            return found, visited, str(error)
        if host(page["url"]) != host(url) and not allowed(page["url"]):
            # The site redirected somewhere that excludes visitors, such as a Facebook page.
            visited.remove(url)
            continue
        for email in extract_emails(page):
            found.setdefault(email, page["url"])
        if url == website:
            queue += contact_links(page)
    return found, visited, None


def company_row(browser, card, delay):
    time.sleep(delay)
    page = read_page(browser, card["contact_url"] or card["profile_url"])
    check_bot(page)
    website = card["website"] if card["website"] and not is_listing(card["website"]) else profile_website(page)
    website = clean_website(website) if website else ""
    found = {email: page["url"] for email in extract_emails(page)}
    failure, status = None, "no website listed"
    if website:
        site_found, visited, failure = website_emails(browser, website, delay)
        found = {**found, **site_found}
        status = (
            f"website failed: {failure}"
            if failure
            else "ok" if found else "no email found" if visited else "robots.txt"
        )
    return {
        "company": card["company"],
        "website": website,
        "emails": "; ".join(found),
        "email_sources": "; ".join(found.values()),
        "profile_url": card["profile_url"],
        "status": status,
    }, failure


def reopen(browser):
    """A page stuck on a dialog or hung script cannot be reused. Never carry it to the next company."""
    try:
        browser.close()
    except (RuntimeError, TimeoutError):
        pass
    return Browser("about:blank", background=False)


def collect(browser, url, writer, *, max_pages, limit, delay):
    """Write one row per company as it finishes, so a stopped run keeps its results."""
    seen, written, page_number = set(), 0, 0
    while url and page_number < max_pages and written < limit:
        page_number += 1
        time.sleep(delay)
        navigate(browser, url)
        results = browser.evaluate(READ_RESULTS)
        check_bot(results)
        if not results["cards"]:
            if page_number == 1:
                raise RuntimeError(f"No company results found on {url}")
            break
        print(f"Page {page_number}: {len(results['cards'])} companies", flush=True)
        for card in results["cards"]:
            if written >= limit:
                break
            if not card["company"] or card["profile_url"] in seen:
                continue
            seen.add(card["profile_url"])
            try:
                row, failure = company_row(browser, card, delay)
            except (RuntimeError, TimeoutError, ValueError) as error:
                if isinstance(error, BotCheck):
                    raise
                row, failure = {
                    "company": card["company"],
                    "website": "",
                    "emails": "",
                    "email_sources": "",
                    "profile_url": card["profile_url"],
                    "status": f"profile failed: {error}",
                }, str(error)
            writer.send(row)
            written += 1
            print(f"{written}. {row['company']}: {row['status']}, {row['emails'] or 'no emails'}", flush=True)
            if failure:
                browser = reopen(browser)
        url = results["next"]
    return browser, written


def csv_writer(handle):
    writer = csv.DictWriter(handle, FIELDS)
    writer.writeheader()
    handle.flush()
    while True:
        writer.writerow((yield))
        handle.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="A thebluebook.com search results URL")
    parser.add_argument("--out", type=Path, default=Path("artifacts/companies.csv"))
    parser.add_argument("--pages", type=int, default=1000, help="Maximum result pages")
    parser.add_argument("--limit", type=int, default=100000, help="Maximum companies")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds before each page visit")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")
    if not is_listing(args.url):
        parser.error("Expected a thebluebook.com search URL")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    browser = Browser("about:blank", background=False)
    written = 0
    try:
        with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv_writer(handle)
            next(writer)
            browser, written = collect(
                browser, args.url, writer, max_pages=args.pages, limit=args.limit, delay=args.delay
            )
    except (RuntimeError, TimeoutError, ValueError) as error:
        sys.exit(f"Stopped: {error}. Results so far are in {args.out}")
    finally:
        try:
            browser.close()
        except (RuntimeError, TimeoutError):
            pass
    print(f"Saved {written} companies to {args.out}")


if __name__ == "__main__":
    main()
