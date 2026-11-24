"""Sign deployed Airflow administrators in with the project's OpenID Connect provider."""

import os
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from airflow.api_fastapi.app import get_cookie_path
from airflow.api_fastapi.auth.managers.base_auth_manager import COOKIE_NAME_JWT_TOKEN
from airflow.api_fastapi.auth.managers.simple.routes.login import create_token, create_token_cli
from airflow.api_fastapi.auth.managers.simple.simple_auth_manager import SimpleAuthManager
from airflow.api_fastapi.auth.managers.simple.user import SimpleAuthManagerUser
from airflow.configuration import conf
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.starlette_client import OAuth
from authlib.oidc.core import UserInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
from starlette.middleware.sessions import SessionMiddleware

from vecten_shared.config import load_config


class OidcUser(SimpleAuthManagerUser):
    """An administrator whose verified email came from the configured issuer."""


class OidcAuthManager(SimpleAuthManager):
    @cached_property
    def config(self):
        return load_config(Path(os.environ["VECTEN_PROJECT_ROOT"]))

    @cached_property
    def admins(self):
        return {email.strip().lower() for email in self.config.mcp.admins if email.strip()}

    def serialize_user(self, user):
        claims = super().serialize_user(user)
        if isinstance(user, OidcUser):
            claims["vecten_oidc"] = True
        else:
            claims["role"] = "viewer"
        return claims

    def deserialize_user(self, token):
        if token.get("vecten_oidc") is True:
            if token["sub"] not in self.admins:
                raise HTTPException(403, "Airflow administrator access required")
            return OidcUser(username=token["sub"], role="admin")
        # This also caps previously issued password sessions when an environment switches.
        return SimpleAuthManagerUser(username=token["sub"], role="viewer", teams=token.get("teams"))

    def get_url_login(self, **kwargs):
        return "/auth/login"

    def get_fastapi_app(self):
        if conf.getboolean("core", "simple_auth_manager_all_admins"):
            raise ValueError("Airflow OIDC requires simple_auth_manager_all_admins to be disabled")
        config = self.config
        issuer = config.mcp.oidc_issuer
        secret_name = config.airflow.oidc_client_secret_name
        secret = os.environ.get(secret_name) if secret_name else None
        if secret_name and not secret:
            raise ValueError(f"Airflow OIDC client secret {secret_name} is empty")
        client = OAuth().register(
            name="vecten",
            server_metadata_url=issuer.rstrip("/") + "/.well-known/openid-configuration",
            client_id=config.airflow.oidc_client_id or config.mcp.oidc_client_id,
            client_secret=secret,
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": "client_secret_basic" if secret else "none",
                "timeout": 8,
            },
        )
        callback_url = os.environ["AIRFLOW__API__BASE_URL"].rstrip("/") + "/auth/oidc/callback"
        app = FastAPI()
        app.add_middleware(
            SessionMiddleware,
            secret_key=os.environ["AIRFLOW__API__SECRET_KEY"],
            session_cookie="__Host-vecten-airflow-oidc",
            https_only=True,
            same_site="lax",
            max_age=300,
        )
        app.add_api_route("/token", create_token, methods=["POST"], status_code=201)
        app.add_api_route("/token/cli", create_token_cli, methods=["POST"], status_code=201)

        async def discovery():
            try:
                metadata = await client.load_server_metadata()
                endpoints = ["authorization_endpoint", "token_endpoint", "jwks_uri"]
                if metadata.get("issuer") != issuer or any(
                    urlsplit(metadata.get(key, "")).scheme != "https" for key in endpoints
                ):
                    raise ValueError("invalid provider metadata")
                # An ID token must carry an asymmetric signature from the issuer's published keys.
                supported = metadata.get("id_token_signing_alg_values_supported", ["RS256"])
                accepted = {
                    "RS256",
                    "RS384",
                    "RS512",
                    "PS256",
                    "PS384",
                    "PS512",
                    "ES256",
                    "ES384",
                    "ES512",
                    "EdDSA",
                }
                metadata["id_token_signing_alg_values_supported"] = [
                    alg for alg in supported if alg in accepted
                ]
                if not metadata["id_token_signing_alg_values_supported"]:
                    raise ValueError("no supported signing algorithm")
            except (httpx2.HTTPError, TypeError, ValueError) as exc:
                raise HTTPException(503, "OpenID Connect provider unavailable") from exc

        @app.get("/login")
        async def login(request: Request):
            await discovery()
            return await client.authorize_redirect(request, callback_url)

        @app.get("/oidc/callback")
        async def callback(request: Request):
            await discovery()
            try:
                token = await client.authorize_access_token(request)
            except (AuthlibBaseError, JoseError, ValueError, TypeError, httpx2.HTTPError) as exc:
                raise HTTPException(401, "OpenID Connect sign-in failed") from exc
            claims = token.get("userinfo")
            if not isinstance(claims, UserInfo) or claims.get("email_verified") is not True:
                raise HTTPException(403, "A verified administrator email is required")
            email = claims.get("email")
            if not isinstance(email, str) or email.strip().lower() not in self.admins:
                raise HTTPException(403, "Airflow administrator access required")
            user = OidcUser(username=email.strip().lower(), role="admin")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                COOKIE_NAME_JWT_TOKEN,
                self.generate_jwt(user),
                path=get_cookie_path(),
                secure=True,
                httponly=True,
                samesite="lax",
            )
            return response

        return app
