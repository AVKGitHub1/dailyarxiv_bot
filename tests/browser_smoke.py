"""Optional real-browser integration check; arXiv and Slack are mocked."""

import contextlib
import io
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from waitress import create_server
from werkzeug.security import generate_password_hash

import slackbot_daily_arxiv as bot
from web_app import create_app


def main():
    config = {"slack_token": "test-only", "channel": "test-only", "cols": [], "categories": [], "subcat": []}
    papers = [
        {"id": "2609.00001", "title": "Programmable interactions in an array of neutral atoms", "authors": ["Ada Lovelace", "Jun Ye"], "abstract": "We investigate programmable interactions in a neutral atom array and explore the resulting quantum dynamics. This paper is a browser test fixture.", "selection": "author", "selected_for": "Jun Ye"},
        {"id": "2609.00002", "title": "A new perspective on quantum simulation with optical tweezers", "authors": ["Grace Hopper", "Emmy Noether"], "abstract": "We present a method for quantum simulation with optical tweezers, and study the control and readout of individual atomic states. This paper is a browser test fixture.", "selection": "keyword", "selected_for": ""},
        {"id": "2609.00003", "title": "Long-lived coherence in a programmable cavity network", "authors": ["Katherine Johnson"], "abstract": "A cavity network provides a setting for studying coherent light–matter interactions. We report a framework for characterizing coherence across the network. This paper is a browser test fixture.", "selection": "keyword", "selected_for": ""},
    ]
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.redirect_stdout(io.StringIO()):
            app = create_app(data_dir=directory, config=config)
        store = app.extensions["store"]
        store.update(lambda state: state.update(password_hash=generate_password_hash("browser-test-password"), baseline_ids=["2609.00001"]))
        server = create_server(app, host="127.0.0.1", port=0, threads=4)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.effective_port}"
        try:
            with patch.object(bot, "build_daily_payload", return_value={"date_str": "2026-09-09", "papers": papers}) as build, patch.object(bot, "post_to_slack", return_value={"ts": "123.456"}) as send, sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=os.environ.get("BROWSER_CHANNEL", "msedge") or None)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                expect(page.locator(".site-header .schedule")).to_contain_text("Schedule paused")
                expect(page.get_by_text("Keep up with what matters.", exact=True)).to_have_count(0)
                expect(page.get_by_text("THE READING ROOM", exact=True)).to_have_count(0)
                expect(page.get_by_text("A LITTLE LESS SEARCHING", exact=False)).to_have_count(0)
                expect(page.get_by_role("heading", name="Last message", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Regenerate")).to_have_count(0)
                expect(page.get_by_text("Regenerate refreshes this list.", exact=False)).to_have_count(0)
                page.get_by_label("Suggest a keyword").fill("Browser smoke keyword")
                page.get_by_role("button", name="Submit keywords suggestion").click()
                expect(page.get_by_text("Suggestion submitted. An admin will review it.", exact=True)).to_be_visible()
                page.get_by_role("link", name="Admin sign in").click()
                page.get_by_label("Password", exact=True).fill("browser-test-password")
                page.get_by_role("button", name="Sign in").click()
                page.get_by_role("button", name="Accept", exact=True).click()
                expect(page.locator("#keywords-list")).to_contain_text("Browser smoke keyword")
                page.get_by_label("Paper date").fill("2026-09-09")
                page.get_by_role("button", name="Regenerate").click()
                expect(page.get_by_text("Preview regenerated. 2 new papers ready to send.", exact=True)).to_be_visible(timeout=20000)
                expect(page.locator("#preview").get_by_text("Click a title to read its abstract.", exact=True)).to_be_visible()
                send.assert_not_called()
                assert "Browser smoke keyword" in build.call_args.kwargs["watchlists"][1]
                page.locator("#preview summary").first.click()
                expect(page.locator("#preview .abstract").first).to_be_visible()
                page.get_by_role("button", name="Send 2 new papers").click()
                expect(page.get_by_text("Sent 2 new papers to Slack.", exact=True)).to_be_visible(timeout=20000)
                expect(page.get_by_role("button", name="Send 0 new papers")).to_be_disabled()
                assert send.call_count == 2
                assert "2609.00001" not in send.call_args_list[0].args[1]
                assert len(store.read()["last_message"]["papers"]) == 2
                page.locator(".digest summary").first.click()
                expect(page.locator(".digest .abstract").first).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                output = ROOT / "data"
                output.mkdir(exist_ok=True)
                page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
                page.get_by_role("button", name="Sign out", exact=True).click()
                expect(page.get_by_role("button", name="Regenerate")).to_have_count(0)
                expect(page.get_by_text("Sent 2 new papers to Slack.", exact=True)).to_have_count(0)
                expect(page.locator(".digest article")).to_have_count(2)
                expect(page.locator(".digest").get_by_text("Click a title to read its abstract.", exact=True)).to_be_visible()
                expect(page.locator("#preview").get_by_text("Click a title to read its abstract.", exact=True)).to_be_visible()
                assert not errors, errors
                browser.close()
                print("Browser checks passed: public suggestions, admin approval, preview-only regeneration, delta send, expandable abstracts, desktop/mobile layout, logout.")
        finally:
            server.close()
            app.extensions["digest_service"].close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
