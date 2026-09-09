"""arxivscraper compatibility with arXiv's current OAI endpoint and bounded I/O."""

import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from arxivscraper import Scraper as LegacyScraper
from arxivscraper.arxivscraper import ARXIV, OAI, Record

ENDPOINT = "https://oaipmh.arxiv.org/oai"


class Scraper(LegacyScraper):
    def scrape(self):
        # Preserve the library's category mapping and config interface while
        # moving both the initial request and pagination to the current API.
        url = ENDPOINT + "?" + urlsplit(self.url).query
        deadline = time.monotonic() + self.timeout
        records = []
        retries = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("arXiv query timed out before all records were retrieved.")
            try:
                request = Request(url, headers={"User-Agent": "DailyArxivBot/1.0 (OAI metadata reader)"})
                with urlopen(request, timeout=min(30, remaining)) as response:
                    xml = response.read(40 * 1024 * 1024 + 1)
                if len(xml) > 40 * 1024 * 1024:
                    raise RuntimeError("arXiv response exceeded the page size limit.")
            except HTTPError as error:
                if error.code not in (429, 503) or retries >= 5:
                    raise
                retries += 1
                try:
                    delay = max(3, int(error.headers.get("Retry-After", self.t)))
                except (ValueError, TypeError):
                    delay = max(3, self.t)
                if time.monotonic() + delay >= deadline:
                    raise TimeoutError("arXiv is busy. Please try again later.") from error
                time.sleep(delay)
                continue
            retries = 0
            root = ET.fromstring(xml)
            error = root.find(OAI + "error")
            if error is not None:
                if error.get("code") == "noRecordsMatch" and not records:
                    return []
                raise RuntimeError(f"arXiv OAI error: {error.get('code', 'unknown')}")
            listing = root.find(OAI + "ListRecords")
            if listing is None:
                raise RuntimeError("arXiv response did not contain a paper list.")
            for node in listing.findall(OAI + "record"):
                header = node.find(OAI + "header")
                if header is not None and header.get("status") == "deleted":
                    continue
                metadata = node.find(OAI + "metadata/" + ARXIV + "arXiv")
                if metadata is None:
                    raise RuntimeError("arXiv returned a record without metadata.")
                record = Record(metadata).output()
                # Keep original scientific capitalization for the webpage.
                for field in ("title", "abstract"):
                    record[field] = " ".join((metadata.findtext(ARXIV + field) or "").split())
                record["authors"] = [
                    " ".join(filter(None, [author.findtext(ARXIV + "forenames"), author.findtext(ARXIV + "keyname")]))
                    for author in metadata.findall(ARXIV + "authors/" + ARXIV + "author")
                ]
                if not record["id"] or not record["title"]:
                    raise RuntimeError("arXiv returned an incomplete paper record.")
                if self.append_all or any(
                    word.lower() in record[key] for key in self.filters for word in self.filters[key]
                ):
                    records.append(record)
            token = listing.findtext(OAI + "resumptionToken")
            if not token:
                return records
            if time.monotonic() + max(3, self.t) >= deadline:
                raise TimeoutError("arXiv query timed out before all pages were retrieved.")
            time.sleep(max(3, self.t))
            url = ENDPOINT + "?" + urlencode({"verb": "ListRecords", "resumptionToken": token})
