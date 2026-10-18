"""The browser chat page at ``GET /chat``.

The page is static: it holds no data or secrets and talks to the authenticated API from the
browser. These tests pin what the server is responsible for (serving it safely, packaging it,
keeping it out of the API contract) and the security-relevant parts of the page itself.
"""

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.api.app import CHAT_PAGE_HEADERS, _chat_page_html, app
from baby.configuration import settings

OP_SECRET = "op-secret-for-tests-0123456789abcdef"
ADMIN_SECRET = "admin-secret-for-tests-0123456789abcdef"


@pytest.fixture
def client():
    with patch.multiple(
        settings,
        api_require_auth=True,
        development_mode=False,
        api_auth_token=OP_SECRET,
        api_admin_token=ADMIN_SECRET,
    ):
        yield TestClient(app)


class TestChatPage:
    def test_served_without_authentication(self, client):
        response = client.get("/chat")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<title>Baby Chat</title>" in response.text

    def test_security_headers(self, client):
        headers = client.get("/chat").headers
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"
        for name, value in CHAT_PAGE_HEADERS.items():
            assert headers[name] == value

    def test_page_contains_no_configured_secrets(self, client):
        body = client.get("/chat").text
        assert OP_SECRET not in body and ADMIN_SECRET not in body

    def test_not_part_of_the_api_contract(self, client):
        assert "/chat" not in client.get("/openapi.json").json()["paths"]

    def test_write_methods_still_rejected(self, client):
        assert client.post("/chat").status_code == 405

    def test_page_loads_from_package_data(self):
        html = _chat_page_html()
        assert html.startswith("<!doctype html>")

    def test_page_only_calls_same_origin_api(self):
        html = _chat_page_html()
        # No third-party scripts, styles or fonts: works offline and leaks nothing.
        assert not re.search(r"<script[^>]+src=", html)
        assert not re.search(r"<link[^>]+stylesheet", html)
        assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in html
        assert 'const API = "/api/v1";' in html

    def test_model_output_is_escaped_before_formatting(self):
        html = _chat_page_html()
        # renderMarkdown escapes every non-code segment, and code blocks, before inserting HTML.
        assert "const lines = esc(part).split" in html
        assert 'esc(body.replace(/\\n$/, ""))' in html
        assert "textContent = text" in html  # the user's own message is never parsed as HTML

    def test_token_kept_per_tab_only(self):
        html = _chat_page_html()
        assert "sessionStorage" in html
        assert "localStorage" not in html
        assert "document.cookie" not in html
