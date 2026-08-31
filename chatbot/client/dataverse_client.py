"""
dataverse_client.py — async OData client for the Eureka Forbes Dataverse org.

Knows nothing about EF business logic: auth, generic GET/POST/PATCH/DELETE, and
consistent errors. Domain logic belongs in chatbot/tools/ef_tools.py.

Token caching is two-tier (the reference project's third tier was Redis):
    in-memory → SQLite kv (survives a restart) → Azure AD

Credentials come from the environment (single tenant):
    DATAVERSE_URL, DATAVERSE_TENANT_ID, DATAVERSE_CLIENT_ID, DATAVERSE_CLIENT_SECRET
"""

import asyncio
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx

from client.store import GLOBAL_NS, SessionStore

logger = logging.getLogger(__name__)

TOKEN_KEY = "dataverse:access_token"
EXPIRY_KEY = "dataverse:token_expiry"
TOKEN_BUFFER_SECONDS = 300  # refresh 5 minutes early


class DataverseError(RuntimeError):
    def __init__(self, status: int, message: str, url: str = ""):
        self.status = status
        self.url = url
        super().__init__(f"[{status}] {message} ({url})")


class DataverseClient:
    _instance: Optional["DataverseClient"] = None

    def __init__(self):
        self.tenant_id = os.getenv("DATAVERSE_TENANT_ID")
        self.client_id = os.getenv("DATAVERSE_CLIENT_ID")
        self.client_secret = os.getenv("DATAVERSE_CLIENT_SECRET")
        self.base_url = (os.getenv("DATAVERSE_URL") or "").rstrip("/")
        self.api_version = os.getenv("DATAVERSE_API_VERSION", "v9.2")

        self._token: Optional[str] = None
        self._expiry: float = 0.0
        self._http: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._store = SessionStore(GLOBAL_NS)

    @classmethod
    def get_client(cls) -> "DataverseClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret and self.base_url)

    @property
    def api_root(self) -> str:
        return f"{self.base_url}/api/data/{self.api_version}"

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    # ==================== AUTH ====================

    def _valid(self, token: Optional[str], expiry: float) -> bool:
        return bool(token) and time.time() < (expiry - TOKEN_BUFFER_SECONDS)

    def _load_cached(self):
        try:
            token = self._store.get(TOKEN_KEY, namespace=GLOBAL_NS)
            expiry = self._store.get(EXPIRY_KEY, namespace=GLOBAL_NS)
            return token, float(expiry) if expiry else 0.0
        except Exception:
            return None, 0.0

    def _store_cached(self, token: str, expiry: float):
        try:
            ttl = max(int(expiry - time.time()), 60)
            self._store.set(TOKEN_KEY, token, ttl=ttl, namespace=GLOBAL_NS)
            self._store.set(EXPIRY_KEY, str(expiry), ttl=ttl, namespace=GLOBAL_NS)
        except Exception as e:
            logger.warning(f"Could not cache Dataverse token: {e}")

    async def invalidate_token(self):
        self._token, self._expiry = None, 0.0
        self._store.delete(TOKEN_KEY, namespace=GLOBAL_NS)
        self._store.delete(EXPIRY_KEY, namespace=GLOBAL_NS)

    async def get_token(self) -> str:
        if self._valid(self._token, self._expiry):
            return self._token

        async with self._lock:
            if self._valid(self._token, self._expiry):
                return self._token

            token, expiry = self._load_cached()
            if self._valid(token, expiry):
                self._token, self._expiry = token, expiry
                return token

            if not self.is_configured():
                raise ValueError(
                    "Dataverse is not configured — set DATAVERSE_URL, DATAVERSE_TENANT_ID, "
                    "DATAVERSE_CLIENT_ID and DATAVERSE_CLIENT_SECRET."
                )

            url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": f"{self.base_url}/.default",
            }
            client = await self._client()
            resp = await client.post(url, data=data)
            if resp.status_code != 200:
                raise DataverseError(resp.status_code, f"Token request failed: {resp.text}", url)

            payload = resp.json()
            self._token = payload["access_token"]
            self._expiry = time.time() + int(payload.get("expires_in", 3600))
            self._store_cached(self._token, self._expiry)
            logger.info("🔑 Dataverse token acquired")
            return self._token

    async def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.get_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Prefer": "odata.include-annotations=\"OData.Community.Display.V1.FormattedValue\"",
        }

    # ==================== REQUESTS ====================

    async def _request(self, method: str, endpoint: str, payload: Optional[dict] = None,
                       retry_auth: bool = True, extra_headers: Optional[dict] = None) -> httpx.Response:
        url = endpoint if endpoint.startswith("http") else f"{self.api_root}/{endpoint.lstrip('/')}"
        client = await self._client()
        headers = await self._headers()
        if extra_headers:
            headers.update(extra_headers)
        resp = await client.request(method, url, headers=headers, json=payload)

        if resp.status_code == 401 and retry_auth:
            logger.warning("🔄 Dataverse 401 — refreshing token and retrying once")
            await self.invalidate_token()
            return await self._request(method, endpoint, payload, retry_auth=False, extra_headers=extra_headers)

        return resp

    async def get(self, endpoint: str) -> dict:
        resp = await self._request("GET", endpoint)
        if resp.status_code >= 400:
            raise DataverseError(resp.status_code, resp.text, endpoint)
        return resp.json()

    async def query(self, entity_set: str, select: Optional[list] = None, filter: Optional[str] = None,
                    top: Optional[int] = None, order_by: Optional[str] = None, expand: Optional[str] = None) -> list:
        """Convenience OData query. Returns the `value` array."""
        params = []
        if select:
            params.append("$select=" + ",".join(select))
        if filter:
            params.append("$filter=" + urllib.parse.quote(filter, safe="()',/ ="))
        if order_by:
            params.append("$orderby=" + urllib.parse.quote(order_by, safe=", "))
        if expand:
            params.append(f"$expand={expand}")
        if top:
            params.append(f"$top={int(top)}")
        endpoint = entity_set + ("?" + "&".join(params) if params else "")
        data = await self.get(endpoint)
        return data.get("value", [])

    async def create(self, entity_set: str, payload: dict, return_record: bool = False):
        """Create a record.

        Returns the new GUID, or the whole created record when return_record is set —
        the only way to read back server-generated values (auto-numbers like
        CASE-000123) without a second round trip.
        """
        headers = {"Prefer": "return=representation"} if return_record else None
        resp = await self._request("POST", entity_set, payload, extra_headers=headers)
        if resp.status_code >= 400:
            raise DataverseError(resp.status_code, resp.text, entity_set)
        if return_record:
            try:
                return resp.json()
            except ValueError:
                pass
        entity_id = resp.headers.get("OData-EntityId", "")
        if "(" in entity_id and ")" in entity_id:
            return entity_id.split("(")[-1].split(")")[0]
        return None

    async def update(self, entity_set: str, record_id: str, payload: dict) -> bool:
        resp = await self._request("PATCH", f"{entity_set}({record_id})", payload)
        if resp.status_code >= 400:
            raise DataverseError(resp.status_code, resp.text, entity_set)
        return True

    async def delete(self, entity_set: str, record_id: str) -> bool:
        resp = await self._request("DELETE", f"{entity_set}({record_id})")
        if resp.status_code >= 400:
            raise DataverseError(resp.status_code, resp.text, entity_set)
        return True

    async def health(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {"configured": False, "reachable": False, "detail": "credentials missing"}
        try:
            await self.get("WhoAmI")
            return {"configured": True, "reachable": True}
        except Exception as e:
            return {"configured": True, "reachable": False, "detail": str(e)}


dataverse = DataverseClient.get_client()
