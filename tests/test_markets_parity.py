"""Other-symbol panes (/api/markets/*): Python's answers to tests/golden/markets/cases.json.

The Rust port (engine-rs/src/markets.rs) must answer the same: server-rs/tests/it/engine_markets.rs
runs the same cases against the same fake helper (tests/golden/markets/fake_feed.mjs) and compares
with answers.json. After a deliberate change, rewrite it with
``UPDATE_MARKETS_GOLDEN=1 uv run pytest tests/test_markets_parity.py``. Needs Node; skipped without it.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wednesday import server
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.markets import Markets
from wednesday.scanner import ScanConfig

GOLDEN = Path(__file__).parent / "golden" / "markets"
NODE = shutil.which("node")


def answers() -> list[dict]:
    cases = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    cfg = ScanConfig(lookback=100)
    out = []
    for clock in cases["clocks"]:
        made: list[Markets] = []
        command = [NODE, str(GOLDEN / "fake_feed.mjs")]
        server_markets = lambda: made.append(Markets(command=lambda: (command, None))) or made[-1]  # noqa: E731
        mp = pytest.MonkeyPatch()
        mp.setattr(server, "Markets", server_markets)
        try:
            engine = Engine(SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100), cfg, "XAUUSD")
            client = TestClient(server.create_app(engine, source="synthetic", clock=clock))
            for route in ("candles", "tick"):
                for query in cases[route]:
                    r = client.get(f"/api/markets/{route}", params=query)
                    out.append({"clock": clock, "route": route, "query": query, "status": r.status_code,
                                "body": r.json()})
        finally:
            mp.undo()
            for m in made:
                m.close()
    return out


@pytest.mark.skipif(NODE is None, reason="node isn't on PATH")
def test_markets_answers_match_the_golden_file():
    got = answers()
    path = GOLDEN / "answers.json"
    if os.environ.get("UPDATE_MARKETS_GOLDEN"):
        path.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == got, (
        "Python's /api/markets answers changed: if on purpose, rerun with UPDATE_MARKETS_GOLDEN=1 "
        "and check server-rs/tests/it/engine_markets.rs still passes")
