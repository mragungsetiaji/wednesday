"""Features a plugin can provide, so the dashboard can show what each one does
even when it isn't installed or licensed.

Everything else in Wednesday is free and needs no licence: the screener,
alerts, the bias, the news card, the manual news brief with your own API key,
labelling and training your own models in the Lab, and one trading journal.
"""

from __future__ import annotations

# id -> title, what it does, the issue that tracks it
CATALOG: dict[str, dict] = {
    "lab.signed_models": {
        "title": "Signed models",
        "description": "Download models trained by momentum.id, signed so Wednesday can verify who made them.",
        "issue": 2,
    },
    "ml.alerts": {
        "title": "Model alerts",
        "description": "Telegram alerts when the active model finds a block above your probability and win chance.",
        "issue": 6,
    },
    "ml.policy": {
        "title": "Take / skip policy",
        "description": "A policy trained on your reviews and trade outcomes that says which order blocks to take.",
        "issue": 11,
    },
    "journal.multi": {
        "title": "Multiple journals",
        "description": "More than one trading journal, e.g. one per MT5 account. One journal is free.",
        "issue": 14,
    },
    "llm.second_brain": {
        "title": "Second brain",
        "description": "Ask questions of your playbook, journal and briefs, answered with sources.",
        "issue": 15,
    },
    "llm.recap": {
        "title": "Session recap",
        "description": "A recap after each session and a weekly review: the bias against what happened, rule breaks, patterns.",
        "issue": 16,
    },
    "llm.scheduled_briefs": {
        "title": "Scheduled briefs",
        "description": "Briefs written before London, before New York and before high-impact news, sent to Telegram.",
        "issue": 17,
    },
}


def catalog(enabled: list[str]) -> list[dict]:
    """Known features with whether a loaded plugin provides them; unknown ids a plugin provides come last."""
    on = set(enabled)
    known = [{"id": fid, **info, "enabled": fid in on} for fid, info in CATALOG.items()]
    extra = [{"id": fid, "title": fid, "description": "", "issue": None, "enabled": True}
             for fid in sorted(on - CATALOG.keys())]
    return known + extra
