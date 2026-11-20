"""Regenerate the README terminal screenshots (assets/*.svg) from a real demo run.

Runs the live chaos demo (`demo-chaos`) with Rich recording enabled, then renders the
tenant's `list-actions` and `metrics` output from the same run, and exports each as SVG.
Only the synthetic `demo` tenant in its own database is used: no real call data, so no
risk of putting personal data into the README.

    docker compose --profile demo build demo
    docker compose run --rm -v ./scripts:/app/scripts:ro -v ./assets:/app/assets \
        demo python scripts/generate_assets.py

The demo database is dropped first so the pictures show exactly one run.
"""

from __future__ import annotations

import io
import sys
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from rich.console import Console  # noqa: E402

from app import config  # noqa: E402
from app.cli import cmd_list_actions, cmd_metrics  # noqa: E402
from app.db import connect  # noqa: E402
from app.demo import TENANT, run_demo  # noqa: E402
from app.tenant_auth import issue_key  # noqa: E402

OUT = ROOT / "assets"


def recorder(width: int) -> Console:
    # Not a terminal: Rich's Live table then prints its final state once instead of
    # recording every refresh frame. Colors are kept because the color system is explicit.
    return Console(record=True, width=width, file=io.StringIO(), color_system="truecolor",
                   force_terminal=False)


def save(console: Console, name: str, title: str) -> Path:
    path = OUT / name
    path.write_text(console.export_svg(title=title), encoding="utf-8")
    return path


def main() -> int:
    settings = config.load()
    demo = replace(settings, database=f"{settings.database}_demo")
    with psycopg.connect(settings.admin_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{demo.database}" WITH (FORCE)')
    OUT.mkdir(exist_ok=True)

    c = recorder(100)
    rc = run_demo(settings, c)
    if rc != 0:
        print(c.export_text())
        print("demo failed; no assets written", file=sys.stderr)
        return rc
    written = [save(c, "demo-chaos.svg", "python -m app.cli demo-chaos")]

    with connect(demo.worker_url) as conn:
        key = issue_key(conn, TENANT, "readme-assets")
    args = Namespace(api_key=key, tenant=None)

    c = recorder(150)
    cmd_list_actions(demo, args, c)
    written.append(save(c, "list-actions.svg", "python -m app.cli list-actions"))

    c = recorder(90)
    cmd_metrics(demo, args, c)
    written.append(save(c, "metrics.svg", "python -m app.cli metrics"))

    for p in written:
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
