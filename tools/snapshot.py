#!/usr/bin/env python3
"""Renders the CTally indicator with demo sessions to PNG, for the README and for checking
the drawing by eye. Runs without a window server (Qt's offscreen platform) unless asked.

    python tools/snapshot.py list out.png
    python tools/snapshot.py all docs/shots --background gradient --opacity 0.8
    python tools/snapshot.py one out.png --scale 1 --background "#808080"

Modes: one (a single badge), three (three badges), list (six sessions), folded (the list
folded to its counts), all (each of those, into a folder). Each shows made-up usage limits
too, unless --no-usage. The demo data is made up: no real sessions, names, paths or usage.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MODES = ("one", "three", "list", "folded")


def demo_sessions():
    from ctally.sessions import Agents, Session, State

    home = os.path.expanduser("~")         # shown as "~", so no real name appears
    return [
        Session("demo-1", State.WORKING, "api", 101, "Migrate to Postgres 16", f"{home}/code/api", "api:0.0",
                Agents(4, started=16, done=12, detail="Verify")),
        Session("demo-2", State.WAITING, "webapp", 102, "fix-flaky-tests", f"{home}/code/webapp", "webapp:0.0"),
        Session("demo-3", State.DONE, "webapp", 103, "Add dark mode toggle", f"{home}/code/webapp", "webapp:0.1"),
        Session("demo-4", State.DONE, "docs", 104, "release-notes", f"{home}/code/docs", "docs:0.0"),
        Session("demo-5", State.WORKING, "api", 105, "Refactor auth middleware", f"{home}/code/api", "api:0.1",
                Agents(1, detail="Find every caller of verifyToken")),
        Session("demo-6", State.IDLE, "playground", 106, "playground", f"{home}/sandbox/playground"),
    ]


def demo_usage(now: float):
    from ctally.usage import Limit, Usage

    hour = 3600
    week = (now // hour + 3 * 24 + 5) * hour            # a few days off, on the hour
    return Usage((
        Limit(("session",), "Current session", "Session", "5h", 64, now + 1.8 * hour),
        Limit(("week",), "Current week (all models)", "Week", "7d", 41, week),
        Limit(("week", "fable"), "Current week (Fable)", "Week · Fable", "", 12, week),
    ), now - 40)


def sessions_for(mode: str):
    sessions = demo_sessions()
    return {"one": sessions[:1], "three": sessions[:3]}.get(mode, sessions)


def render(mode: str, out: Path, background: str, opacity: float, ratio: float, usage: bool = True) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRegion
    from PySide6.QtWidgets import QWidget

    from ctally.gui.view import TallyView

    view = TallyView()
    view.apply(sessions_for(mode))
    if usage:
        import time
        view.apply_usage(demo_usage(time.time()))
    view.folded = mode == "folded"
    if hasattr(view, "settle"):
        view.settle()                       # no entry animations mid-flight
    size = view.preferred_size()
    view.resize(size)

    # Rendered into a transparent image, as the translucent window would show it, at the
    # pixel ratio asked for.
    format_ = QImage.Format.Format_ARGB32_Premultiplied
    shot = QImage(round(size.width() * ratio), round(size.height() * ratio), format_)
    shot.setDevicePixelRatio(ratio)
    shot.fill(Qt.GlobalColor.transparent)
    view.render(shot, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)

    margin = round(24 * ratio)
    image = QImage(shot.width() + 2 * margin, shot.height() + 2 * margin, format_)
    painter = QPainter(image)
    if background == "none":
        image.fill(Qt.GlobalColor.transparent)
    elif background == "gradient":
        # Something like a desktop wallpaper: CTally has to stay legible over colour.
        gradient = QLinearGradient(QPointF(0, 0), QPointF(image.width(), image.height()))
        gradient.setColorAt(0, QColor("#1d1b3a"))
        gradient.setColorAt(0.55, QColor("#3a2350"))
        gradient.setColorAt(1, QColor("#7a3f5c"))
        painter.fillRect(image.rect(), gradient)
    else:
        image.fill(QColor(background))
    painter.setOpacity(opacity)             # what the window's opacity setting does on screen
    shot.setDevicePixelRatio(1)             # pixel for pixel onto the backdrop
    painter.drawImage(margin, margin, shot)
    painter.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(out)):
        sys.exit(f"couldn't write {out}")
    print(f"{out}: {image.width()}x{image.height()} ({mode}, {len(sessions_for(mode))} sessions)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parser.add_argument("mode", choices=MODES + ("all",))
    parser.add_argument("out", type=Path, help="the PNG to write (a folder for 'all')")
    parser.add_argument("--background", default="#585858",
                        help="a colour (#rrggbb), 'gradient' for a wallpaper-like one, or 'none'")
    parser.add_argument("--opacity", type=float, default=1.0, help="the indicator's opacity (the app's default is 0.8)")
    parser.add_argument("--scale", type=float, default=2.0, help="device pixel ratio (2 for Retina)")
    parser.add_argument("--onscreen", action="store_true", help="use the real window system instead of offscreen")
    parser.add_argument("--no-usage", action="store_true", help="leave out the usage limits")
    args = parser.parse_args()

    # Set before Qt starts.
    if not args.onscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts.warning=false")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])        # widgets need it alive while they render

    if args.mode == "all":
        for mode in MODES:
            render(mode, args.out / f"{mode}.png", args.background, args.opacity, args.scale, not args.no_usage)
    else:
        render(args.mode, args.out, args.background, args.opacity, args.scale, not args.no_usage)
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
