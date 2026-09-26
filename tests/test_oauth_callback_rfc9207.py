"""Tests for RFC 9207 iss parameter handling in OAuth callbacks.

These tests verify that the `iss` parameter is correctly parsed and forwarded
through callback relay paths, matching the behavior required by MCP 2.x.
"""

import pytest
from scripts.oauth_callback import parse_oauth_callback, OAuthCallbackResult


class TestRFC9207IssParameter:
    """Tests for RFC 9207 issuer identifier handling."""

    def test_parses_iss_when_present(self):
        """Cloudflare/Resend send iss; it must be extracted and preserved."""
        url = "http://localhost:1234/callback?code=abc123&state=st-1&iss=https://mcp.cloudflare.com"
        result = parse_oauth_callback(url)
        
        assert result.code == "abc123"
        assert result.state == "st-1"
        assert result.error is None
        assert result.iss == "https://mcp.cloudflare.com"

    def test_iss_absent_returns_none_not_missing(self):
        """Providers not advertising RFC 9207 omit iss; must round-trip as None."""
        url = "http://localhost:1234/callback?code=abc123&state=st-1"
        result = parse_oauth_callback(url)
        
        assert result.code == "abc123"
        assert result.state == "st-1"
        assert result.error is None
        assert result.iss is None  # Not undefined, not dropped — explicitly None

    def test_error_response_includes_iss_none(self):
        """Error responses must also include iss=None for consistent shape."""
        url = "http://localhost:1234/callback?error=access_denied&state=st-1"
        result = parse_oauth_callback(url)
        
        assert result.code is None
        assert result.state == "st-1"
        assert result.error == "access_denied"
        assert result.iss is None

    def test_unparseable_url_returns_error_with_iss_none(self):
        """Malformed URLs must not crash; iss=None preserves return shape."""
        result = parse_oauth_callback("not-a-valid-url")
        
        assert result.code is None
        assert result.state is None
        assert result.error == "unparseable callback URL"
        assert result.iss is None

    def test_tuple_unpacking_preserves_iss(self):
        """Legacy call sites using tuple unpacking receive iss in third position."""
        url = "http://localhost:1234/callback?code=abc&state=xyz&iss=https://auth.example.com"
        code, state, iss = parse_oauth_callback(url)
        
        assert code == "abc"
        assert state == "xyz"
        assert iss == "https://auth.example.com"

    def test_tuple_unpacking_without_iss_returns_none(self):
        """Legacy call sites get None for iss when provider omits it."""
        url = "http://localhost:1234/callback?code=abc&state=xyz"
        code, state, iss = parse_oauth_callback(url)
        
        assert code == "abc"
        assert state == "xyz"
        assert iss is None

    def test_dataclass_immutability(self):
        """Result should be immutable to prevent accidental mutation in relays."""
        result = parse_oauth_callback("http://localhost/callback?code=x&state=y&iss=z")
        with pytest.raises(AttributeError):
            result.iss = "tampered"  # type: ignore[misc]

    def test_iss_with_special_characters_url_encoded(self):
        """iss values may contain URL-special chars; must be properly decoded."""
        # iss = "https://auth.example.com/path?query=value" -> encoded in redirect
        encoded_iss = "https%3A%2F%2Fauth.example.com%2Fpath%3Fquery%3Dvalue"
        url = f"http://localhost/callback?code=abc&state=xyz&iss={encoded_iss}"
        result = parse_oauth_callback(url)
        
        assert result.iss == "https://auth.example.com/path?query=value"

    def test_extra_parameters_ignored(self):
        """Unknown query parameters must not interfere with known ones."""
        url = "http://localhost/callback?code=abc&state=xyz&iss=https://a.com&extra=ignored&foo=bar"
        result = parse_oauth_callback(url)
        
        assert result.code == "abc"
        assert result.state == "xyz"
        assert result.iss == "https://a.com"
        assert result.error is None


class TestBackwardCompatibility:
    """Ensure existing call sites without iss awareness continue working."""

    def test_legacy_three_tuple_unpacking_still_works(self):
        """Code expecting (code, state) or (code, state, error) must not break."""
        # This simulates a call site that only knows about code/state
        url = "http://localhost/callback?code=abc&state=xyz"
        result = parse_oauth_callback(url)
        
        # Old-style: code, state = result (would fail with 3-tuple)
        # New-style: code, state, iss = result (works, iss=None)
        code, state, iss = result
        assert code == "abc"
        assert state == "xyz"
        assert iss is None

    def test_attribute_access_preferred_over_indexing(self):
        """Named attributes are clearer and safer than tuple indexing."""
        result = parse_oauth_callback("http://localhost/callback?code=abc&state=xyz&iss=https://a.com")
        
        assert result.code == "abc"
        assert result.state == "xyz"
        assert result.iss == "https://a.com"
        assert result.error is None
