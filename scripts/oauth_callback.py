"""OAuth callback parsing utilities for MCP 2.x RFC 9207 compliance.

This module provides a standardized way to parse OAuth authorization responses,
ensuring the `iss` (issuer) parameter is always extracted and forwarded.
MCP 2.x requires `iss` when the authorization server advertises
`authorization_response_iss_parameter_supported` (e.g., Cloudflare, Resend).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class OAuthCallbackResult:
    """Parsed OAuth authorization response parameters.

    Attributes:
        code: Authorization code from the provider (None if error).
        state: State parameter for CSRF protection (None if missing).
        error: Error code from the provider (None if success).
        iss: RFC 9207 issuer identifier (None if not provided by server).
    """

    code: str | None
    state: str | None
    error: str | None
    iss: str | None

    @classmethod
    def from_url(cls, callback_url: str) -> OAuthCallbackResult:
        """Parse an OAuth callback URL into its components.

        Args:
            callback_url: The full redirect URL received from the auth server.

        Returns:
            OAuthCallbackResult with all parameters extracted.
            Missing parameters are None (not omitted) to preserve round-trip
            compatibility with providers that don't advertise RFC 9207.
        """
        try:
            parsed = urlparse(callback_url)
            params = parse_qs(parsed.query)

            # parse_qs returns lists; take first value or None
            def first(key: str) -> str | None:
                vals = params.get(key)
                return vals[0] if vals else None

            code = first("code")
            error = first("error")
            # A callback carrying neither a code nor an error carries no
            # outcome at all. urlparse is very forgiving -- "not-a-valid-url"
            # parses happily into an empty query -- so this case never
            # reached the except branch below and came back looking like
            # success with code=None. Callers follow the documented
            # `if result.error: raise` contract, so they walked on with a
            # None code and failed later and less clearly. Treat it as the
            # unparseable input it is, keeping the return shape (iss=None).
            if code is None and error is None:
                return cls(
                    code=None,
                    state=first("state"),
                    error="unparseable callback URL",
                    iss=None,
                )

            return cls(
                code=code,
                state=first("state"),
                error=error,
                iss=first("iss"),
            )
        except Exception:
            # Unparseable URL — treat as error, preserve None for iss
            return cls(
                code=None, state=None, error="unparseable callback URL", iss=None
            )

    def to_tuple(self) -> tuple[str | None, str | None, str | None]:
        """Return (code, state, iss) for legacy call sites expecting 3-tuple.

        Note: error is intentionally omitted; callers should check .error directly.
        """
        return (self.code, self.state, self.iss)

    def __iter__(self):
        """Allow tuple unpacking: code, state, iss = result"""
        return iter(self.to_tuple())


def parse_oauth_callback(callback_url: str) -> OAuthCallbackResult:
    """Convenience function for one-line parsing.

    Example:
        result = parse_oauth_callback("http://localhost:1234/callback?code=abc&state=xyz&iss=https://auth.example.com")
        if result.error:
            raise OAuthError(result.error)
        # result.code, result.state, result.iss available
    """
    return OAuthCallbackResult.from_url(callback_url)
