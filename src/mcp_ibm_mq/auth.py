"""OAuth access-token verification for the streamable HTTP transport.

The MCP server acts as an OAuth 2.1 resource server: the client gets an
access token from your identity provider (Entra ID, Okta, Ping, Keycloak...)
and sends it as a bearer token. This verifier checks it before any tool runs:

* signature, against the provider's published keys (JWKS); "none" and
  symmetric algorithms are never accepted,
* issuer, so tokens from another provider are refused,
* audience, so a token issued for a different MCP server or API is refused,
* expiry and not-before, with a small clock leeway.

From the claims it builds the Identity for the call:

* agent  = ``azp`` or ``client_id`` (the registered agent), or the actor in an
  RFC 8693 ``act`` claim when the token came from a token exchange,
* user   = ``sub`` when it differs from the agent (the person the agent acts for),
* scopes = ``scope`` / ``scp``, keeping only mq.read, mq.write, mq.consume.

Needs the ``auth`` extra: ``pip install "mcp-ibm-mq-server[auth]"``.
"""

from __future__ import annotations

from typing import Any

from .identity import Identity, parse_scopes

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384")


class JWTVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        jwks: dict[str, Any] | None = None,
        algorithms: tuple[str, ...] = ALLOWED_ALGORITHMS,
        leeway_seconds: int = 30,
    ):
        import jwt  # imported lazily: only needed for HTTP with auth

        if not (jwks_url or jwks):
            raise ValueError("Provide jwks_url (or a jwks dict for tests).")
        bad = [a for a in algorithms if a.startswith("HS") or a.lower() == "none"]
        if bad:
            raise ValueError(f"Refusing insecure algorithms: {bad}")
        self._jwt = jwt
        self.issuer = issuer
        self.audience = audience
        self.algorithms = list(algorithms)
        self.leeway = leeway_seconds
        self._jwks_client = jwt.PyJWKClient(jwks_url) if jwks_url else None
        self._jwks = jwt.PyJWKSet.from_dict(jwks) if jwks else None

    def _key(self, token: str):
        if self._jwks_client is not None:
            return self._jwks_client.get_signing_key_from_jwt(token).key
        kid = self._jwt.get_unverified_header(token).get("kid")
        for jwk in self._jwks.keys:
            if jwk.key_id == kid:
                return jwk.key
        raise self._jwt.InvalidTokenError(f"No signing key with kid '{kid}'.")

    def decode(self, token: str) -> dict[str, Any]:
        """Verify the token and return its claims. Raises jwt.InvalidTokenError on any problem."""
        header = self._jwt.get_unverified_header(token)
        if header.get("alg") not in self.algorithms:
            raise self._jwt.InvalidAlgorithmError(f"Algorithm '{header.get('alg')}' is not allowed.")
        return self._jwt.decode(
            token,
            self._key(token),
            algorithms=self.algorithms,
            audience=self.audience,
            issuer=self.issuer,
            leeway=self.leeway,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )

    @staticmethod
    def identity_from_claims(claims: dict[str, Any]) -> Identity:
        act = claims.get("act") if isinstance(claims.get("act"), dict) else None
        agent = (act or {}).get("sub") or claims.get("azp") or claims.get("client_id")
        sub = claims.get("sub")
        if not agent:
            agent = sub  # client-credentials token: the agent is the subject
        user = sub if sub and sub != agent else None
        scopes = parse_scopes(claims.get("scope") if "scope" in claims else claims.get("scp"))
        return Identity(agent_id=str(agent), user_id=user, scopes=scopes, source="token")

    def identify(self, token: str) -> Identity:
        return self.identity_from_claims(self.decode(token))

    async def verify_token(self, token: str):
        """MCP SDK TokenVerifier hook: return an AccessToken, or None to reject with 401."""
        from mcp.server.auth.provider import AccessToken

        try:
            claims = self.decode(token)
        except self._jwt.PyJWTError:
            return None
        who = self.identity_from_claims(claims)
        return AccessToken(
            token=token,
            client_id=who.agent_id,
            scopes=sorted(who.scopes),
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=claims.get("sub"),
            claims=claims,
        )
