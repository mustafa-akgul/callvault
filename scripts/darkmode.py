"""Dark variants of the light diagrams, made by remapping the palette.

Every figure is drawn once in the light palette; `to_dark` swaps each colour for its role in
a GitHub-dark palette, so the two variants can never drift apart. The README picks one with
<picture> and `prefers-color-scheme`.
"""

from __future__ import annotations

import re

DARK = {
    "#faf8f5": "#0d1117",  # page
    "#ffffff": "#161b22",  # card
    "#f3efe8": "#1c2230",  # quiet fill
    "#ddd6ca": "#30363d",  # hairline
    "#1f2328": "#e6edf3",  # ink
    "#5f6670": "#9198a1",  # muted ink
    "#3b5b92": "#79a6e8",  # data
    "#eef2f8": "#14213a",  # data tint
    "#2f7d4f": "#46b565",  # ok
    "#eef6f0": "#11271a",  # ok tint
    "#c2410c": "#f0883e",  # chaos
    "#fff4ec": "#2b1c12",  # chaos tint
}


def to_dark(svg: str) -> str:
    return re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: DARK.get(m.group(0).lower(), m.group(0)), svg)
