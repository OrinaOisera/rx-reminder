"""Single source of truth for patient colours.

Colour is never the only signal. Callers must show the patient name and
initial next to the colour. Calendar titles use the emoji plus the name.
"""

PALETTE = (
    {"colorKey": "red", "emoji": "🟥", "hex": "#E53935", "cssName": "red"},
    {"colorKey": "orange", "emoji": "🟧", "hex": "#FB8C00", "cssName": "darkorange"},
    {"colorKey": "yellow", "emoji": "🟨", "hex": "#F9A825", "cssName": "goldenrod"},
    {"colorKey": "green", "emoji": "🟩", "hex": "#43A047", "cssName": "green"},
    {"colorKey": "blue", "emoji": "🟦", "hex": "#1E88E5", "cssName": "dodgerblue"},
    {"colorKey": "purple", "emoji": "🟪", "hex": "#8E24AA", "cssName": "purple"},
    {"colorKey": "brown", "emoji": "🟫", "hex": "#6D4C41", "cssName": "saddlebrown"},
    {"colorKey": "grey", "emoji": "⬛", "hex": "#708090", "cssName": "slategray"},
)

_BY_KEY = {item["colorKey"]: item for item in PALETTE}


def palette_list() -> list[dict]:
    return [dict(item) for item in PALETTE]


def get_color(color_key: str) -> dict | None:
    found = _BY_KEY.get(color_key)
    return dict(found) if found else None


def first_unused(used: set[str]) -> str | None:
    for item in PALETTE:
        if item["colorKey"] not in used:
            return item["colorKey"]
    return None
