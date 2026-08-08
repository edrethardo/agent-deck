from __future__ import annotations

import argparse

from . import paths


def _run_daemon() -> int:
    import asyncio
    import logging
    import signal

    from .config import load_pad_config
    from .daemon import Daemon
    from .pad import DeepDeckPad
    from .scan import claude_processes
    from .state import SessionRegistry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_pad_config(paths.config_path())
    if cfg is None:
        logging.info("no pad configured (%s) — running without hardware", paths.config_path())
    try:
        daemon = Daemon(SessionRegistry(), None, paths.state_path(), paths.socket_path(),
                        scan_fn=claude_processes)
        pad = DeepDeckPad(cfg, on_focus=daemon.focus_slot, on_move=daemon.move_slot) if cfg else None
        daemon._pad = pad

        async def _main() -> None:
            # systemd stops with SIGTERM: cancel the daemon task so run()'s
            # cleanup (cancel + gather of background tasks) actually executes.
            task = asyncio.ensure_future(daemon.run())
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, task.cancel)
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_main())
    except (RuntimeError, OSError) as exc:
        # e.g. another daemon already owns the socket, or the runtime dir
        # isn't usable — exit cleanly, non-zero, instead of a traceback.
        logging.error("%s", exc)
        return 1
    return 0


async def _test_pattern_async() -> int:
    import asyncio

    from .config import load_pad_config
    from .pad import DeepDeckPad
    from .render import NUM_KEY_LEDS

    cfg = load_pad_config(paths.config_path())
    if cfg is None:
        print(f"No pad configured — create {paths.config_path()} (see README).")
        return 1
    pad = DeepDeckPad(cfg)
    task = asyncio.create_task(pad.run())
    print(f"Connecting to {cfg.host} ...")
    if not await pad.wait_connected(30):
        print("Pad unreachable.")
        task.cancel()
        return 1
    print("Chase: one green LED walks across all keys (check the order!)")
    # Full brightness on purpose — a visibility aid, not the production BRIGHTNESS scaling.
    for i in range(NUM_KEY_LEDS):
        colors = [0] * (NUM_KEY_LEDS * 3)
        colors[i * 3 + 1] = 255
        await pad.show(colors, [f"Chase key {i + 1}"], [""] * NUM_KEY_LEDS, [0] * NUM_KEY_LEDS)
        await asyncio.sleep(0.3)
    for name, rgb in [("green", (0, 255, 0)), ("yellow", (255, 160, 0)),
                      ("red", (255, 0, 0)), ("off", (0, 0, 0))]:
        print(f"All keys: {name}")
        await pad.show(list(rgb) * NUM_KEY_LEDS, [f"Test: {name}"], [""] * NUM_KEY_LEDS, [0] * NUM_KEY_LEDS)
        await asyncio.sleep(1.0)
    task.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="run the daemon (via systemd)")
    sub.add_parser("hook", help="Claude Code hook entry point (reads stdin)")
    status = sub.add_parser("status", help="show session status")
    status.add_argument("--watch", action="store_true", help="refresh live")
    sub.add_parser("test-pattern", help="play an LED test pattern on the pad")
    args = parser.parse_args(argv)

    if args.cmd == "daemon":
        return _run_daemon()
    if args.cmd == "hook":
        from . import hook
        return hook.main()
    if args.cmd == "status":
        from .statusview import run_status
        return run_status(args.watch)
    if args.cmd == "test-pattern":
        import asyncio

        from .statusview import daemon_running

        if daemon_running():
            print("Stop the daemon first — it is pushing to the pad too: "
                  "systemctl --user stop agent-monitor")
            return 1
        return asyncio.run(_test_pattern_async())
    return 2
