import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from arxiv_source import Scraper


def document(body):
    return f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">{body}</OAI-PMH>'.encode()


def page(token=""):
    return document('''<ListRecords><record><header/><metadata>
      <arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>2609.00001</id>
      <title>Quantum DNA</title><abstract>An Abstract.</abstract>
      <categories>physics.atom-ph quant-ph</categories>
      <authors><author><keyname>Lovelace</keyname><forenames>Ada</forenames></author></authors>
      </arXiv></metadata></record>''' + f'<resumptionToken>{token}</resumptionToken></ListRecords>')


class SourceTests(unittest.TestCase):
    def test_current_endpoint_pagination_escaping_and_case_preservation(self):
        with patch("arxiv_source.urlopen", side_effect=[io.BytesIO(page("a/b+c=")), io.BytesIO(page())]) as open_url, patch("arxiv_source.time.sleep"):
            records = Scraper("physics", "2026-09-09", "2026-09-09").scrape()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["title"], "Quantum DNA")
        self.assertEqual(records[0]["authors"], ["Ada Lovelace"])
        self.assertTrue(open_url.call_args_list[0].args[0].full_url.startswith("https://oaipmh.arxiv.org/oai?"))
        self.assertIn("resumptionToken=a%2Fb%2Bc%3D", open_url.call_args_list[1].args[0].full_url)
        self.assertLessEqual(open_url.call_args.kwargs["timeout"], 30)

    def test_empty_result_and_protocol_errors_are_distinct(self):
        with patch("arxiv_source.urlopen", return_value=io.BytesIO(document('<error code="noRecordsMatch">empty</error>'))):
            self.assertEqual(Scraper("physics").scrape(), [])
        for xml in [document('<error code="badArgument"/>'), b"<html/>"]:
            with patch("arxiv_source.urlopen", return_value=io.BytesIO(xml)), self.assertRaises(RuntimeError):
                Scraper("physics").scrape()

    def test_filters_and_deleted_records(self):
        with patch("arxiv_source.urlopen", return_value=io.BytesIO(page())):
            self.assertEqual(Scraper("physics", filters={"categories": ["physics.optics"]}).scrape(), [])
        with patch("arxiv_source.urlopen", return_value=io.BytesIO(document('<ListRecords><record><header status="deleted"/></record></ListRecords>'))):
            self.assertEqual(Scraper("physics").scrape(), [])

    def test_transient_errors_honor_retry_after_and_are_bounded(self):
        error = HTTPError("https://oaipmh.arxiv.org", 503, "busy", {"Retry-After": "10"}, None)
        with patch("arxiv_source.urlopen", side_effect=error) as open_url, patch("arxiv_source.time.sleep") as sleep:
            with self.assertRaises(HTTPError):
                Scraper("physics").scrape()
        self.assertEqual(open_url.call_count, 6)
        sleep.assert_called_with(10)

    def test_timeout_never_returns_partial_results(self):
        with patch("arxiv_source.urlopen", return_value=io.BytesIO(page("next"))), patch("arxiv_source.time.monotonic", side_effect=[0, 0, 299]):
            with self.assertRaises(TimeoutError):
                Scraper("physics", timeout=300).scrape()
