#!/usr/bin/env python3
"""Scarica dal modem le pagine usate dall'integrazione e mostra cosa ci legge.

Serve per verificare (o correggere) i parser su un firmware specifico senza
dover riavviare Home Assistant:

    pip install aiohttp beautifulsoup4
    python3 tools/dump_pages.py 192.168.0.1 admin --dump-dir /tmp/timhub

La password viene chiesta a runtime e non viene mai salvata. Con --dump-dir
l'HTML grezzo di ogni pagina viene salvato su file, utile da allegare a una
segnalazione se un campo non viene riconosciuto.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import sys
import types
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "tim_hub_plus"

# api.py non dipende da Home Assistant, ma il package sì: lo si importa da solo.
_package = types.ModuleType("tim_hub_plus_standalone")
_package.__path__ = [str(PACKAGE_DIR)]
sys.modules["tim_hub_plus_standalone"] = _package
api = importlib.import_module("tim_hub_plus_standalone.api")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="indirizzo IP del modem, es. 192.168.0.1")
    parser.add_argument("username", nargs="?", default="admin")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--dump-dir", type=Path, help="dove salvare l'HTML grezzo")
    args = parser.parse_args()

    password = getpass.getpass(f"Password di {args.username}@{args.host}: ")
    client = api.TimHubClient(args.host, args.port, args.username, password)

    try:
        await client.login()
        print("Login riuscito.\n")

        paths = {"devices": api.DEVICES_PATH, **api.SETTINGS_PATHS}
        for name, path in paths.items():
            try:
                html = await client._get_page(path)
            except api.TimHubError as err:
                print(f"[{name}] {path}: ERRORE {err}")
                continue

            print(f"[{name}] {path}: {len(html)} byte")
            if args.dump_dir:
                args.dump_dir.mkdir(parents=True, exist_ok=True)
                (args.dump_dir / f"{name}.html").write_text(html, encoding="utf-8")

        print("\n== Dispositivi rilevati")
        for device in await client.get_devices():
            print(
                f"  {device.mac} | nome={device.name!r} ip={device.ip!r} "
                f"collegamento={device.interface!r} connesso={device.connected}"
            )

        print("\n== Impostazioni riconosciute")
        settings = await client.get_settings()
        for name, value in vars(settings).items():
            if name not in ("raw", "errors"):
                print(f"  {name} = {value!r}")

        print("\n== Campi grezzi letti da ogni pagina")
        for page, fields in settings.raw.items():
            print(f"  [{page}]")
            for key, value in fields.items():
                print(f"    {key} = {value!r}")
        for page, error in settings.errors.items():
            print(f"  [{page}] ERRORE: {error}")
    finally:
        await client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
