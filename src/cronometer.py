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
        for raw in resp.text.splitlines():
            line = raw[6:].strip() if raw.startswith("data: ") else raw.strip()
            if line.startswith("{"):
                msg = json.loads(line)
                if "result" in msg or "error" in msg:
                    return msg
        raise CronometerError(f"malformed bridge response: {resp.text[:200]}")

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
            raise CronometerError(f"{tool}: {msg['error']}")
        result = msg["result"]
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
                raise CronometerError(f"{tool}: {inner[:200]}") from None
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            raise CronometerError(f"{tool}: {json.dumps(data)[:200]}")
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
            raise CronometerError(f"add_custom_food returned no ids: {json.dumps(res)[:200]}")
        return {"food_id": int(res["food_id"]), "measure_id": int(res["measure_id"])}

    def find_custom_food(self, name: str) -> dict | None:
        """Find an existing *custom* food by exact name, or None.

        Recovery path: the recipe -> food mapping lives in grocy-cook's data
        volume, so without this a lost volume would create a duplicate custom
        food for every recipe (Cronometer has no delete-food API). Only
        source="Custom" hits count — a curated database entry with a similar
        name would carry different macros.
        """
        try:
            res = self.call("search_foods", {"query": name[:200]})
        except CronometerError:
            return None
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
    ) -> str | None:
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
        return str(eid) if eid is not None else None

    def remove_food_entry(self, entry_id: str) -> None:
        self.call("remove_food_entry", {"entry_ids": [str(entry_id)]})
