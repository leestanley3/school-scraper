import asyncio
import importlib
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "synthetic-test-key"}):
    main = importlib.import_module("main")

SCHOOL = {"account_id": "123", "school_name": "Synthetic school", "staff_page_url": "https://example.invalid/staff"}


class SecurityTests(unittest.TestCase):
    def test_both_write_endpoints_fail_closed(self):
        client = TestClient(main.app)
        for secret in ("", " ", "changeme123", "configured-test-secret"):
            for supplied in (None, "wrong", "changeme123", "non-ascii-\u00e9"):
                with self.subTest(secret=secret, supplied=supplied), patch.object(main, "WEBHOOK_SECRET", secret), patch.object(main, "process_school", new_callable=AsyncMock) as process:
                    headers = {} if supplied is None else {"x-webhook-secret": supplied.encode("utf-8")}
                    for path, data in (("/scrape", SCHOOL), ("/scrape/bulk", {"schools": [SCHOOL]})):
                        response = client.post(path, json=data, headers=headers)
                        self.assertEqual(response.status_code, 401 if secret == "configured-test-secret" else 503)
                    process.assert_not_called()

    def test_authorized_request_still_works_without_logging_payload(self):
        with patch.object(main, "WEBHOOK_SECRET", "configured-test-secret"), patch.object(main, "process_school", new_callable=AsyncMock, return_value={"status": "ok"}) as process, redirect_stdout(io.StringIO()) as output:
            client = TestClient(main.app)
            for payload in ({"json": SCHOOL}, {"data": SCHOOL}):
                response = client.post("/scrape", headers={"x-webhook-secret": "configured-test-secret"}, **payload)
                self.assertEqual(response.json(), {"status": "ok"})
            self.assertEqual(process.await_count, 2)
            self.assertEqual(output.getvalue(), "")

    def test_provider_failure_does_not_leak_secrets(self):
        with patch.object(main, "fetch_page", new_callable=AsyncMock, side_effect=RuntimeError("private-token-and-personal-data")), self.assertLogs(main.logger, level="ERROR") as logs:
            result = asyncio.run(main.process_school(main.ScrapeRequest(**SCHOOL)))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("traceback", result)
        self.assertNotIn("private-token", str(result) + str(logs.output))

    def test_failed_contact_search_cannot_create_duplicate(self):
        for status, payload in ((401, {"error": "unauthorized"}), (200, {"data": None}), (200, {"error": "bad response"})):
            with self.subTest(status=status, payload=payload):
                response = httpx.Response(status, json=payload, request=httpx.Request("GET", "https://example.invalid"))
                client = AsyncMock()
                client.get.return_value = response
                client.__aenter__.return_value = client
                with patch.object(main.httpx, "AsyncClient", return_value=client), self.assertRaises(Exception):
                    asyncio.run(main.upsert_contact("123", {"first_name": "Test", "last_name": "Person"}, "senco", {}, "test-token"))
                client.post.assert_not_called()
                client.put.assert_not_called()

    def test_explicit_no_contacts_result_is_allowed(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(204, request=httpx.Request("GET", "https://example.invalid"))
        client.__aenter__.return_value = client
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            self.assertEqual(asyncio.run(main.get_existing_contacts("123", "test-token")), [])


if __name__ == "__main__":
    unittest.main()
