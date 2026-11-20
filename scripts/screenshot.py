#!/usr/bin/env python3
"""Capture REAL screenshots (+ a GIF) of the running guardrail-agent UI.

Drives the live app in headless Chromium via Playwright:
  1. home page (clean state after /reset)
  2. approval card visible (send-email task paused for a human)
  3. approved run (email in the MOCK outbox, audit log populated)
The GIF is stitched (PIL) from genuine frames captured during that flow —
nothing is mocked up or drawn by hand.

Usage: python scripts/screenshot.py [--base http://127.0.0.1:8002]
Outputs: screenshots/01-home.png, 02-approval.png, 03-approved.png, demo.gif
"""

import argparse
import io
import os
import time

import httpx
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "screenshots")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8002")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    os.makedirs(OUT, exist_ok=True)

    from playwright.sync_api import sync_playwright

    client = httpx.Client(base_url=base, timeout=60)
    client.post("/reset")
    frames: list[Image.Image] = []

    def snap(page, name: str):
        path = os.path.join(OUT, name)
        page.screenshot(path=path)
        print("saved", path)
        with open(path, "rb") as fh:
            frames.append(Image.open(io.BytesIO(fh.read())).convert("RGB"))
        return path

    def agent_count(page):
        return page.evaluate("document.querySelectorAll('.msg.agent').length")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        snap(page, "01-home.png")

        # Safe task first, so the audit log is non-empty in later shots.
        # Wait for a NEW agent message (the greeting already counts as one).
        before = agent_count(page)
        page.fill("#msg", "What is on my calendar tomorrow?")
        page.click("button.primary")
        page.wait_for_function(
            f"document.querySelectorAll('.msg.agent').length > {before}",
            timeout=600000,
        )
        page.wait_for_timeout(1000)
        snap(page, "01b-calendar-answer.png")

        # Sensitive task -> approval card. One task at a time: the previous
        # run is finished before this one starts.
        before = agent_count(page)
        page.fill("#msg", "Send an email to Priya about the project update")
        page.click("button.primary")
        print("waiting for approval card (LLM planning on CPU can take a few minutes)...")
        page.wait_for_selector("#approval.show", timeout=600000)
        page.wait_for_timeout(800)
        snap(page, "02-approval.png")

        # Approve it, then show the result.
        before = agent_count(page)
        page.click("#btnApprove")
        page.wait_for_function(
            f"document.querySelectorAll('.msg.agent').length > {before}",
            timeout=300000,
        )
        page.wait_for_timeout(1500)
        snap(page, "03-approved.png")
        browser.close()

    # GIF from the genuine frames (home -> calendar -> approval -> approved).
    if frames:
        gif_path = os.path.join(OUT, "demo.gif")
        frames[0].save(
            gif_path, save_all=True, append_images=frames[1:],
            duration=1200, loop=0, optimize=True,
        )
        print("saved", gif_path, f"({len(frames)} frames)")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
