"""meross/core.py — control Meross smart plugs via the vendor cloud.

Wraps the async `meross_iot` library in a sync CLI. Credentials live in a
gitignored file at the project root; the resulting auth token is cached so
subsequent CLI calls skip the HTTP-login round-trip.

Files:
    meross_creds.local.json   — user credentials (gitignored, YOU create this)
        { "email": "...", "password": "...", "api_base_url": null }
    meross_token.local.json   — cached auth token (gitignored, auto-managed)

Matching: `--on <name>` matches the Meross device name case-insensitively.
Use `--list` to see the exact names as the Meross app reports them.

Usage:
    meross --list
    meross --status <name>
    meross --on     <name>
    meross --off    <name>
    meross --toggle <name>
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Optional

from meross_iot.controller.mixins.electricity import ElectricityMixin
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager
from meross_iot.model.credentials import MerossCloudCreds

LOG_PATH = Path(tempfile.gettempdir()) / "hardware-bar-meross.log"
log = logging.getLogger("meross")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDS_FILE   = PROJECT_ROOT / "meross_creds.local.json"
TOKEN_FILE   = PROJECT_ROOT / "meross_token.local.json"

# Region-specific API endpoints: iotx-eu / iotx-ap / iotx-us. Meross
# auto-redirects to the correct regional shard based on account; starting
# on the nearest one shaves ~300ms off cold-start. Australia → AP.
DEFAULT_API_BASE = "https://iotx-ap.meross.com"


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(process)5d] %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass
    # Quiet meross_iot's own chatty loggers and stop them bubbling up to any
    # root handler the library installed via logging.basicConfig() elsewhere.
    log.propagate = False
    for noisy in ("meross_iot", "paho", "asyncio", "aiohttp"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.ERROR)
        lg.propagate = False


# -------- credentials / token cache ------------------------------------

def _load_creds() -> dict:
    if not CREDS_FILE.exists():
        raise RuntimeError(
            f"missing {CREDS_FILE.name}. Create it at the project root with:\n"
            '    {"email": "you@example.com", "password": "..."}\n'
        )
    data = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
    if not data.get("email") or not data.get("password"):
        raise RuntimeError(f"{CREDS_FILE.name} must include 'email' and 'password'")
    return data


def _load_cached_token() -> Optional[MerossCloudCreds]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return MerossCloudCreds.from_json(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("cached token unreadable, discarding: %s", e)
        return None


def _save_token(creds: MerossCloudCreds) -> None:
    try:
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    except OSError as e:
        log.warning("failed to persist token: %s", e)


async def _connect() -> tuple[MerossHttpClient, MerossManager]:
    cached = _load_cached_token()
    http = None
    if cached is not None:
        try:
            http = await MerossHttpClient.async_from_cloud_creds(cached)
            log.info("using cached auth token")
        except Exception as e:
            log.warning("cached token failed (%s); re-logging in", e)
    if http is None:
        creds = _load_creds()
        http = await MerossHttpClient.async_from_user_password(
            email=creds["email"],
            password=creds["password"],
            api_base_url=creds.get("api_base_url") or DEFAULT_API_BASE,
        )
        _save_token(http.cloud_credentials)

    manager = MerossManager(http_client=http)
    await manager.async_init()
    await manager.async_device_discovery()
    return http, manager


# -------- device helpers -----------------------------------------------

def _match(plug, query: str) -> bool:
    return (plug.name.casefold() == query.casefold()
            or plug.uuid.casefold() == query.casefold())


async def _instant_power(plug) -> Optional[float]:
    if not isinstance(plug, ElectricityMixin):
        return None
    try:
        metrics = await plug.async_get_instant_metrics()
        return float(metrics.power)
    except Exception as e:
        log.debug("power read failed on %s: %s", plug.name, e)
        return None


async def _do(action: str, name: Optional[str]) -> int:
    http, manager = await _connect()
    try:
        plugs = manager.find_devices()
        if not plugs:
            print("(no devices discovered on this account)")
            return 1

        if action == "list":
            for p in sorted(plugs, key=lambda d: d.name.casefold()):
                try:
                    await p.async_update()
                except Exception:
                    pass
                online = getattr(p.online_status, "name", str(p.online_status))
                online_mark = "ok" if online == "ONLINE" else online.lower()
                try:
                    on = p.is_on(channel=0)
                except Exception:
                    on = None
                state = "ON " if on else "OFF" if on is False else "?  "
                watts = await _instant_power(p)
                power = f"{watts:5.1f}W" if watts is not None else "   --"
                print(f"  [{state}] {power}  {online_mark:<10}  {p.name}")
            return 0

        if name is None:
            print("this command needs a device name (try --list)")
            return 2

        target = next((p for p in plugs if _match(p, name)), None)
        if target is None:
            print(f"no device named {name!r}. Try --list.")
            return 1

        try:
            await target.async_update()
        except Exception as e:
            log.warning("update failed for %s: %s", target.name, e)

        if action == "status":
            cur = target.is_on(channel=0)
            watts = await _instant_power(target)
            suffix = f"  ({watts:.1f}W)" if watts is not None else ""
            print(("on" if cur else "off") + suffix)
            return 0
        if action == "on":
            await target.async_turn_on(channel=0)
            print("on")
            return 0
        if action == "off":
            await target.async_turn_off(channel=0)
            print("off")
            return 0
        if action == "toggle":
            cur = target.is_on(channel=0)
            if cur:
                await target.async_turn_off(channel=0)
                print("off")
            else:
                await target.async_turn_on(channel=0)
                print("on")
            return 0

        print(f"unknown action {action!r}")
        return 2
    finally:
        try:
            manager.close()
        except Exception:
            pass
        # Deliberately do NOT call http.async_logout() — that invalidates our
        # cached token on Meross's side and forces a slow fresh login every
        # time. The token's good for hours; let subsequent CLI calls reuse it.


# -------- CLI ----------------------------------------------------------

def main() -> int:
    _setup_logging()
    args = sys.argv[1:]
    log.info("=" * 50)
    log.info("launch argv=%s log=%s", args, LOG_PATH)

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if args[0] == "--list":
        return asyncio.run(_do("list", None))
    if args[0] in ("--on", "--off", "--toggle", "--status"):
        if len(args) < 2:
            print(f"{args[0]} needs a device name (quote it if it contains spaces)")
            return 2
        # Join remaining args so "Office Plant" works without quoting
        name = " ".join(args[1:])
        return asyncio.run(_do(args[0][2:], name))

    print(f"unknown command: {args[0]}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
