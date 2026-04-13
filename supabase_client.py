"""Shared Supabase PostgREST and Storage client for plan2bid-worker."""
import json
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BATCH_SIZE = 100

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

_client = httpx.Client(headers=HEADERS, timeout=30)


_CONTROL_PARAMS = {"select", "order", "limit", "offset", "on_conflict"}

def get(table, **params):
    """GET with auto eq. prefix on filter params. Control params (select, order, limit) pass through."""
    processed = {}
    for k, v in params.items():
        if k in _CONTROL_PARAMS:
            processed[k] = v  # Pass through as-is
        else:
            processed[k] = f"eq.{v}"  # Auto-prefix filter values
    resp = _client.get(f"{SUPABASE_URL}/rest/v1/{table}", params=processed)
    resp.raise_for_status()
    return resp.json()


def post(table, data):
    """Insert rows. Accepts dict or list of dicts."""
    if isinstance(data, dict):
        data = [data]
    h = {**HEADERS, "Prefer": "return=representation"}
    for i in range(0, len(data), BATCH_SIZE):
        resp = _client.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data[i:i+BATCH_SIZE], headers=h)
        resp.raise_for_status()


def upsert(table, data, on_conflict):
    """Upsert a single row."""
    h = {**HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"}
    resp = _client.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data, headers=h, params={"on_conflict": on_conflict})
    resp.raise_for_status()


def patch(table, data, **filters):
    """Update rows matching filters. Returns updated rows."""
    h = {**HEADERS, "Prefer": "return=representation"}
    params = {k: f"eq.{v}" for k, v in filters.items()}
    resp = _client.patch(f"{SUPABASE_URL}/rest/v1/{table}", json=data, headers=h, params=params)
    resp.raise_for_status()
    return resp.json() if resp.content else []


def delete(table, **filters):
    """Delete rows matching filters."""
    h = {**HEADERS, "Prefer": "return=minimal"}
    params = {k: f"eq.{v}" for k, v in filters.items()}
    resp = _client.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params)
    resp.raise_for_status()


def download_storage(bucket, path):
    """Download file bytes from Supabase Storage."""
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    resp = _client.get(url)
    resp.raise_for_status()
    return resp.content


def close():
    """Close the httpx client connection pool."""
    _client.close()
