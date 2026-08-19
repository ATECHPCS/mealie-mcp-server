"""Client for the Cronometer MCP bridge (streamable HTTP).

The bridge already holds the Cronometer credentials, so this server talks to
it rather than to Cronometer directly (ported from grocy-cook). Only a few
tools are used:

  add_custom_food   -> creates a food, returns food_id + measure_id
  add_food_entry    -> logs grams of that food on a date, returns an entry id
  remove_food_entry -> undo, used when a later step fails

Quirks worth keeping: the bridge wraps its JSON payload in a `result` string,
and remove_food_entry takes `entry_ids` as a list of STRINGS.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class CronometerError(RuntimeError):
    pass


def _sanitize(detail: Any) -> str:
    """A short, safe error string — never a raw bridge/food payload.

    Bridge responses can carry user/food data; the repo rule forbids logging it,
    and these messages flow into logs. Keep only a bounded, single-line summary.
    """
    s = " ".join(str(detail).split())
    return s[:120] if s else "unknown error"


class Cronometer:
    def __init__(self, url: str, token: str, timeout: float = 60.0) -> None:
        if not url:
            raise CronometerError("cronometer MCP url is not configured")
        self._url = url
        self._c = httpx.Client(timeout=timeout)
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._id = 0
        self._started = False

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> "Cronometer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, body: dict) -> httpx.Response:
        try:
            return self._c.post(self._url, headers=self._headers, json=body)
        except httpx.HTTPError as e:
            raise CronometerError(f"cronometer bridge unreachable: {e}") from e

    def _start(self) -> None:
        """MCP handshake; the bridge hands back a session id we must echo."""
        if self._started:
            return
        r = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "grocy-cook", "version": "1"},
                },
            }
        )
        if r.status_code >= 400:
            raise CronometerError(f"cronometer initialize HTTP {r.status_code}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._headers["mcp-session-id"] = sid
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._started = True

    @staticmethod
    def _payload(resp: httpx.Response) -> dict:
        # Accept plain JSON and SSE ("data:" with or without a space). Try each
        # standalone JSON line first, then the assembled SSE data, then the whole
        # body (pretty-printed JSON).
        def _try(text: str):
            text = text.strip()
            if text.startswith("{"):
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    return None
                if "result" in msg or "error" in msg:
                    return msg
            return None

        data_parts = []
        for raw in resp.text.splitlines():
            line = raw.strip()
            if line.startswith("data:"):
                data_parts.append(line[5:].lstrip())
                continue
            hit = _try(line)
            if hit is not None:
                return hit
        for candidate in ("".join(data_parts), resp.text):
            hit = _try(candidate)
            if hit is not None:
                return hit
        # don't echo the body — it may carry user/food data (repo logging rule)
        raise CronometerError(f"malformed bridge response (HTTP {resp.status_code})")

    def call(self, tool: str, args: dict) -> Any:
        self._start()
        msg = self._payload(
            self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": args},
                }
            )
        )
        if "error" in msg:
            raise CronometerError(f"{tool}: {_sanitize(msg['error'])}")
        result = msg["result"]
        # MCP tools report execution failures via isError on the result, not the
        # JSON-RPC error field — treat that as a hard failure.
        if isinstance(result, dict) and result.get("isError"):
            raise CronometerError(f"{tool}: bridge returned an error result")
        data = result.get("structuredContent") or {}
        if not data:
            for c in result.get("content", []):
                if c.get("type") == "text":
                    data = {"result": c["text"]}
                    break
        # the bridge nests its real payload as a JSON string under "result"
        inner = data.get("result")
        if isinstance(inner, str):
            try:
                data = json.loads(inner)
            except json.JSONDecodeError:
                raise CronometerError(f"{tool}: unparseable payload") from None
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            # surface only the status/error string, never the whole payload
            detail = _sanitize(data.get("error") or data.get("message") or data.get("status"))
            raise CronometerError(f"{tool}: {detail}")
        return data

    # ---- the three operations we need ----
    def add_custom_food(self, name: str, macros: dict, serving_grams: float) -> dict:
        res = self.call(
            "add_custom_food",
            {
                "name": name[:200],
                "calories": macros["calories"],
                "protein_g": macros["protein_g"],
                "fat_g": macros["fat_g"],
                "carbs_g": macros["carbs_g"],
                "fiber_g": macros.get("fiber_g", 0),
                "sugar_g": macros.get("sugar_g", 0),
                "sodium_mg": macros.get("sodium_mg", 0),
                "saturated_fat_g": macros.get("saturated_fat_g", 0),
                "serving_name": "1 serving",
                "serving_grams": serving_grams,
            },
        )
        if not res.get("food_id") or not res.get("measure_id"):
            raise CronometerError("add_custom_food returned no ids")
        return {"food_id": int(res["food_id"]), "measure_id": int(res["measure_id"])}

    def find_custom_food(self, name: str) -> dict | None:
        """Find an existing *custom* food by exact name, or None.

        Without this a lost mapping would create a duplicate custom food for
        every recipe (Cronometer has no delete-food API). Only source="Custom"
        hits count — a curated database entry with a similar name would carry
        different macros.

        Raises CronometerError on a bridge failure. Returning None here means a
        confirmed no-match, NOT "the search failed" — a swallowed error would
        make the caller create a duplicate food on every transient outage.
        """
        res = self.call("search_foods", {"query": name[:200]})
        target = name.strip().lower()
        for f in res.get("foods") or []:
            if (
                str(f.get("source") or "").lower() == "custom"
                and str(f.get("name") or "").strip().lower() == target
                and f.get("food_id")
                and f.get("measure_id")
            ):
                return {"food_id": int(f["food_id"]), "measure_id": int(f["measure_id"])}
        return None

    def add_food_entry(
        self,
        food_id: int,
        measure_id: int,
        grams: float,
        date: str | None = None,
        diary_group: str | None = None,
    ) -> str:
        args: dict[str, Any] = {
            "food_id": int(food_id),
            "measure_id": int(measure_id),
            "grams": grams,
        }
        if date:
            args["date"] = date
        if diary_group:
            args["diary_group"] = diary_group
        res = self.call("add_food_entry", args)
        entry = res.get("entry") or {}
        eid = entry.get("id")
        if eid is None:
            # a "success" status with no entry id is not a real log — never let
            # the caller report success on it.
            raise CronometerError("add_food_entry returned no entry id")
        return str(eid)

    def remove_food_entry(self, entry_id: str) -> None:
        self.call("remove_food_entry", {"entry_ids": [str(entry_id)]})
