"""Offline tests for Blue Book collection. No browser, network, or model calls."""

from unittest.mock import Mock

import httpx
import pytest

from jev_ultrafast import bluebook


def web_page(url="https://acme.com/", text="", mailto=(), links=(), title="Acme"):
    return {
        "url": url,
        "title": title,
        "text": text,
        "mailto": list(mailto),
        "links": [{"href": href, "text": label} for href, label in links],
    }


def card(number, website="https://acme.com/?utm_source=thebluebook&utm_medium=proview"):
    return {
        "company": f"Company {number}",
        "profile_url": f"https://www.thebluebook.com/iProView/{number}",
        "contact_url": f"https://www.thebluebook.com/iProView/{number}/contact/",
        "website": website,
    }


class FakeBrowser:
    def __init__(self, pages, results):
        self.pages, self.results, self.url, self.visited = pages, list(results), None, []

    def goto(self, url):
        self.url = url
        self.visited.append(url)

    def evaluate(self, expression):
        if expression == bluebook.READ_RESULTS:
            return self.results.pop(0)
        return self.pages.get(self.url, web_page(self.url))


@pytest.fixture
def rows():
    collected = []

    def writer():
        while True:
            collected.append((yield))

    generator = writer()
    next(generator)
    return collected, generator


@pytest.fixture(autouse=True)
def open_robots(monkeypatch):
    monkeypatch.setattr(bluebook, "allowed", lambda _url: True)


def test_emails_come_from_mailto_and_text_without_junk():
    page = web_page(
        text="Reach Sales@Acme.com. Logo logo@2x.png, sample jane@example.com, tracker a@o1.sentry.io",
        mailto=["mailto:office%40acme.com?subject=Bid", "MAILTO:a@acme.com,b@acme.com"],
    )
    assert bluebook.extract_emails(page) == ["office@acme.com", "a@acme.com", "b@acme.com", "sales@acme.com"]


def test_listing_site_addresses_are_not_company_emails():
    assert bluebook.extract_emails(web_page(text="help@thebluebook.com info@mail.construction.com")) == []


def test_main_location_wins_over_other_branches():
    page = {
        **web_page(),
        "locations": [
            {"main": False, "address": "1 Branch Rd., Reno, NV 89501"},
            {"main": True, "address": "2 Main St., Suite 7, Fresno, CA 93723"},
        ],
    }
    assert bluebook.main_address(page) == "2 Main St., Suite 7, Fresno, CA 93723"


def test_first_location_is_used_when_none_is_marked_main():
    page = {**web_page(), "locations": [{"main": False, "address": "1 Branch Rd., Reno, NV 89501"}]}
    assert bluebook.main_address(page) == "1 Branch Rd., Reno, NV 89501"


def test_address_comes_only_from_location_cards():
    assert bluebook.main_address(web_page(text="Headquarters: 56 Broad Street Boston, MA 02109")) == ""


def test_address_reaches_the_row_even_without_a_website(rows):
    collected, writer = rows
    contact = card(1)["contact_url"]
    profile = {**web_page(contact), "locations": [{"main": True, "address": "2 Main St., Fresno, CA 93723"}]}
    browser = FakeBrowser({contact: profile}, [{"title": "", "text": "", "cards": [card(1, website="")], "next": ""}])
    bluebook.collect(browser, "start", writer, max_pages=1, limit=9, delay=0)
    assert collected[0]["address"] == "2 Main St., Fresno, CA 93723"
    assert collected[0]["status"] == "no website listed"


def test_header_city_is_used_when_there_is_no_location_card():
    page = web_page(text="Burdick Excavating Co., Inc.\nMound House, NV 89721\nPre-Qual\nHome")
    assert bluebook.main_address(page) == "Mound House, NV 89721"


def test_footer_address_is_never_taken_for_the_company():
    filler = "\n".join(["Services and capabilities listed here"] * 30)
    page = web_page(text=f"Acme\n{filler}\nHeadquarters:\n56 Broad Street\nBoston, MA 02109")
    assert bluebook.main_address(page) == ""


def test_generic_contact_link_goes_through_the_company_profile(rows):
    collected, writer = rows
    profile_url = "https://www.thebluebook.com/iProView/7"
    contact = "https://www.thebluebook.com/iProView/7/armac/subcontractors/locations-contacts/"
    listing = {
        **card(7, website=""),
        "profile_url": profile_url,
        "contact_url": "https://www.thebluebook.com/locations-contacts/",
    }
    pages = {
        profile_url: web_page(profile_url, links=[(contact, "Contact Us")]),
        contact: {
            **web_page(contact, links=[("https://armac.com/", "Website")]),
            "locations": [{"main": True, "address": "343 Sairview Dr., Carson City, NV 89701"}],
        },
    }
    browser = FakeBrowser(pages, [{"title": "", "text": "", "cards": [listing], "next": ""}])
    bluebook.collect(browser, "start", writer, max_pages=1, limit=9, delay=0)
    assert "https://www.thebluebook.com/locations-contacts/" not in browser.visited
    assert collected[0]["address"] == "343 Sairview Dr., Carson City, NV 89701"
    assert collected[0]["website"] == "https://armac.com/"


def test_contact_links_stay_on_site_and_prefer_contact_pages():
    page = web_page(
        links=[
            ("https://acme.com/about", "About"),
            ("https://acme.com/get-in-touch", "Contact Us"),
            ("https://facebook.com/acme/about", "About"),
            ("https://acme.com/team.pdf", "Team"),
            ("https://acme.com/#contact", "Contact"),
            ("https://acme.com/services", "Services"),
        ]
    )
    assert bluebook.contact_links(page) == ["https://acme.com/get-in-touch", "https://acme.com/about"]


def test_website_keeps_real_query_but_drops_tracking():
    url = "https://acme.com/home?id=7&utm_source=thebluebook&UTM_campaign=x#top"
    assert bluebook.clean_website(url) == "https://acme.com/home?id=7"


def test_only_blue_book_hosts_count_as_listings():
    assert bluebook.is_listing("http://Acme.thebluebook.com")
    assert not bluebook.is_listing("https://notthebluebook.com")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, "User-agent: *\nDisallow: /contact", False),
        (200, "User-agent: *\nDisallow: /private", True),
        (404, "", True),
        (403, "", True),
        (503, "", False),
    ],
)
def test_company_robots_rules_are_applied(monkeypatch, status, body, expected):
    monkeypatch.undo()
    monkeypatch.setattr(bluebook.httpx, "get", Mock(return_value=httpx.Response(status, text=body)))
    assert bluebook.allowed("https://acme.com/contact") is expected


def test_unreachable_robots_rules_keep_the_tool_out(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(bluebook.httpx, "get", Mock(side_effect=httpx.ConnectError("offline")))
    assert bluebook.allowed("https://acme.com/") is False


def test_momentary_robots_failure_is_tried_again(monkeypatch):
    monkeypatch.undo()
    responses = [httpx.ConnectError("blip"), httpx.Response(200, text="User-agent: *\n")]
    monkeypatch.setattr(bluebook.httpx, "get", Mock(side_effect=responses))
    assert bluebook.allowed("https://acme.com/") is True


def test_redirect_to_a_site_that_excludes_visitors_is_not_read(monkeypatch):
    monkeypatch.setattr(bluebook, "allowed", lambda url: "facebook" not in url)
    landed = web_page("https://www.facebook.com/acme", text="owner@acme.com")
    browser = FakeBrowser({"https://acme.com/": landed}, [])
    assert bluebook.website_emails(browser, "https://acme.com/", 0) == ({}, [], None)


def test_redirect_to_a_new_company_domain_is_still_read():
    landed = web_page("https://acme-group.com/", text="office@acme-group.com")
    browser = FakeBrowser({"https://acme.com/": landed}, [])
    found, _, failure = bluebook.website_emails(browser, "https://acme.com/", 0)
    assert found == {"office@acme-group.com": "https://acme-group.com/"}
    assert failure is None


def test_aborted_navigation_is_tried_once_more(monkeypatch):
    monkeypatch.setattr(bluebook.time, "sleep", lambda _seconds: None)
    browser = Mock()
    browser.goto.side_effect = ["net::ERR_ABORTED", None]
    bluebook.navigate(browser, "https://acme.com/")
    assert browser.goto.call_count == 2


def test_navigation_errors_are_not_retried_endlessly(monkeypatch):
    monkeypatch.setattr(bluebook.time, "sleep", lambda _seconds: None)
    browser = Mock()
    browser.goto.return_value = "net::ERR_ABORTED"
    with pytest.raises(RuntimeError, match="ERR_ABORTED"):
        bluebook.navigate(browser, "https://acme.com/")
    assert browser.goto.call_count == 2


def test_other_navigation_errors_are_reported_without_retry():
    browser = Mock()
    browser.goto.return_value = "net::ERR_NAME_NOT_RESOLVED"
    with pytest.raises(RuntimeError, match="NAME_NOT_RESOLVED"):
        bluebook.navigate(browser, "https://acme.com/")
    assert browser.goto.call_count == 1


def test_goto_does_not_treat_the_old_page_as_loaded(monkeypatch):
    import jev_ultrafast.browser as browser_module

    browser = object.__new__(browser_module.Browser)
    browser.call = Mock(return_value={})
    states = iter(["old", "old", "loading", "complete"])
    evaluated = []

    def evaluate(expression):
        evaluated.append(expression)
        return True if "= true" in expression else next(states)

    browser.evaluate = evaluate
    assert browser.goto("https://acme.com/") is None
    assert len(evaluated) == 5
    browser.call.assert_called_once()


def test_goto_reports_a_navigation_that_never_replaced_the_page(monkeypatch):
    import jev_ultrafast.browser as browser_module

    clock = iter([0, 0, 99])
    monkeypatch.setattr(browser_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(browser_module.time, "sleep", lambda _seconds: None)
    browser = object.__new__(browser_module.Browser)
    browser.call = Mock(return_value={})
    browser.evaluate = Mock(return_value="old")
    assert browser.goto("https://acme.com/") == "Navigation did not finish"


def test_robots_exclusion_is_reported_without_visiting_the_site(monkeypatch, rows):
    monkeypatch.setattr(bluebook, "allowed", lambda _url: False)
    browser = FakeBrowser({}, [{"title": "", "text": "", "cards": [card(1)], "next": ""}])
    bluebook.collect(browser, "start", rows[1], max_pages=1, limit=9, delay=0)
    assert rows[0][0]["status"] == "robots.txt"
    assert "https://acme.com/" not in browser.visited
    assert rows[0][0]["website"] == "https://acme.com/"


def test_collect_follows_pages_removes_duplicates_and_records_sources(rows):
    collected, writer = rows
    contact = "https://acme.com/contact"
    browser = FakeBrowser(
        {
            "https://acme.com/": web_page(links=[(contact, "Contact")]),
            contact: web_page(contact, text="bids@acme.com"),
        },
        [
            {"title": "", "text": "", "cards": [card(1)], "next": "page2"},
            {"title": "", "text": "", "cards": [card(1), card(2, website="")], "next": ""},
        ],
    )
    _, written = bluebook.collect(browser, "page1", writer, max_pages=5, limit=9, delay=0)
    assert written == 2
    assert collected[0]["emails"] == "bids@acme.com"
    assert collected[0]["email_sources"] == contact
    assert collected[0]["website"] == "https://acme.com/"
    assert collected[1]["status"] == "no website listed"


def test_limit_stops_collection(rows):
    browser = FakeBrowser({}, [{"title": "", "text": "", "cards": [card(1), card(2)], "next": "more"}])
    _, written = bluebook.collect(browser, "start", rows[1], max_pages=5, limit=1, delay=0)
    assert written == 1
    assert len(browser.visited) == 3


def test_bot_check_stops_the_run_instead_of_continuing(rows):
    browser = FakeBrowser({}, [{"title": "Just a moment...", "text": "", "cards": [card(1)], "next": ""}])
    with pytest.raises(bluebook.BotCheck):
        bluebook.collect(browser, "start", rows[1], max_pages=1, limit=9, delay=0)
    assert rows[0] == []


def test_bot_check_on_a_profile_keeps_earlier_rows(rows):
    collected, writer = rows
    pages = {card(2)["contact_url"]: web_page(card(2)["contact_url"], title="Attention Required")}
    browser = FakeBrowser(pages, [{"title": "", "text": "", "cards": [card(1), card(2)], "next": ""}])
    with pytest.raises(bluebook.BotCheck):
        bluebook.collect(browser, "start", writer, max_pages=1, limit=9, delay=0)
    assert [row["company"] for row in collected] == ["Company 1"]


def test_failed_company_site_keeps_going_in_a_fresh_browser(monkeypatch, rows):
    collected, writer = rows
    replacement = FakeBrowser({}, [])
    monkeypatch.setattr(bluebook, "reopen", lambda _browser: replacement)
    browser = FakeBrowser({}, [{"title": "", "text": "", "cards": [card(1)], "next": ""}])
    real_goto = browser.goto

    def goto(url):
        real_goto(url)
        if url == "https://acme.com/":
            raise TimeoutError("Page.navigate timed out")

    browser.goto = goto
    current, written = bluebook.collect(browser, "start", writer, max_pages=1, limit=9, delay=0)
    assert written == 1
    assert current is replacement
    assert collected[0]["status"] == "website failed: Page.navigate timed out"
