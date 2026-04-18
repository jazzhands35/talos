"""Repro: does the SchedulePopup render without crashing for realistic input?"""
from __future__ import annotations

import asyncio
import sys
import traceback

from textual.app import App, ComposeResult

from talos.models.tree import ArbPairRecord
from talos.ui.schedule_popup import SchedulePopup


def _mk_record(et: str, series: str, title: str = "") -> ArbPairRecord:
    return ArbPairRecord(
        event_ticker=et + "-M",
        ticker_a=et + "-M",
        ticker_b=et + "-M",
        kalshi_event_ticker=et,
        series_ticker=series,
        category="Politics",
        sub_title=title,
    )


class _Host(App[None]):
    def compose(self) -> ComposeResult:  # pragma: no cover
        return iter([])


async def _run() -> None:
    records = [
        _mk_record("KXTRUMPSAY-26APR20-MARI", "KXTRUMPSAY", "Trump says MARI"),
        _mk_record("KXTRUMPSAY-26APR20-50", "KXTRUMPSAY", "Trump says 50"),
        _mk_record("KXTRUTHSOCIAL-26APR18-B169", "KXTRUTHSOCIAL", "Truth Social"),
    ]

    # Run push_screen_wait inside a worker — same shape as the real fix in
    # action_commit_changes. Without this, it raises NoActiveWorker.
    app = _Host()
    async with app.run_test() as pilot:
        result_holder: list[object] = []

        async def _in_worker() -> None:
            popup = SchedulePopup(records)
            r = await app.push_screen_wait(popup)
            result_holder.append(r)

        try:
            app.run_worker(_in_worker(), exclusive=True)
            await pilot.pause()
            await pilot.click("#cancel")
            # Wait briefly for the worker to resolve dismissal
            for _ in range(20):
                if result_holder:
                    break
                await pilot.pause()
            print(f"POPUP_RETURNED: {result_holder!r}")
        except Exception:
            print("POPUP_CRASHED:")
            traceback.print_exc()
            sys.exit(1)
        else:
            print("POPUP_OK")


if __name__ == "__main__":
    asyncio.run(_run())
