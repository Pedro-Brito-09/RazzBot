#!/usr/bin/env python3
"""Inspect one Open Cloud data store entry: is it a buffer, and what's in it?

    python3 tools/inspect_entry.py <datastore> <key>

Reads API_KEY from the environment, so source the service env file first:

    sudo bash -c 'set -a; . /etc/razzbot.env; set +a; \
        /opt/razzbot/.venv/bin/python /opt/razzbot/tools/inspect_entry.py \
        Leaderboards 9234'

The verdict line is the point: the game stores map and Daily Cup boards as
Luau buffers, and the Open Cloud data store API can report a buffer but never
store one -- so anything written through that API reads back as a plain table
and the game fails on buffer.tostring. Use this to tell the two apart.

Ordered data stores (Wins, Medals, Creator Points) are a different API and are
not covered here.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

# Falls back to the live universe the bot uses.
DEFAULT_UNIVERSE_ID = os.getenv("UNIVERSE_ID") or "8993151589"
PREVIEW_ROWS = 5


def parse_args():
    parser = argparse.ArgumentParser(
        description="Show the stored type and contents of a data store entry."
    )
    parser.add_argument("datastore", help='e.g. Leaderboards, Main_Data')
    parser.add_argument("key", help='e.g. 9234, DailyCup_192, TodaysMap')
    parser.add_argument(
        "-u", "--universe", default=DEFAULT_UNIVERSE_ID,
        help=f"universe ID (default {DEFAULT_UNIVERSE_ID})",
    )
    parser.add_argument(
        "--dev", action="store_true",
        help="use DEV_UNIVERSE_ID instead",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="print the whole decoded value, not just the first rows",
    )
    return parser.parse_args()


def fetch(universe, datastore, key, api_key):
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/{quote(str(universe))}"
        f"/data-stores/{quote(datastore, safe='')}"
        f"/entries/{quote(str(key), safe='')}"
    )
    print(f"GET {url}\n")
    request = urllib.request.Request(url, headers={
        "x-api-key": api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:500]
        sys.exit(f"HTTP {error.code}: {body}")
    except urllib.error.URLError as error:
        sys.exit(f"request failed: {error.reason}")


def buffer_field(value):
    """Roblox's buffer payload field, or None when it isn't one."""
    if not isinstance(value, dict):
        return None
    for field in ("zbase64", "base64"):
        if isinstance(value.get(field), str):
            return field
    return None


def unwrap(value, *, max_depth=6):
    """Peel every buffer layer. Returns (decoded, layers, undecodable bytes).

    An entry can be wrapped more than once -- a buffer whose contents are the
    JSON of another buffer envelope -- so this keeps going until what comes
    out is no longer one.
    """
    layers = []
    current = value
    for _ in range(max_depth):
        field = buffer_field(current)
        if field is None:
            return current, layers, None
        layers.append((field, current.get("t")))
        decoded, raw_bytes = decode(current, field)
        if decoded is None:
            return None, layers, raw_bytes
        current = decoded
    return current, layers, None


def decode(value, field):
    raw = base64.b64decode(value[field])
    if field == "zbase64":
        try:
            import zstandard as zstd
        except ImportError:
            sys.exit(
                "this entry is zstd-compressed; run it with the bot's "
                "interpreter: /opt/razzbot/.venv/bin/python"
            )
        import io
        decompressor = zstd.ZstdDecompressor()
        try:
            raw = decompressor.decompress(raw)
        except zstd.ZstdError:
            # Frames without the content size in the header need streaming.
            raw = decompressor.stream_reader(io.BytesIO(raw)).read()
    try:
        return json.loads(raw), None
    except ValueError:
        return None, raw


def main():
    args = parse_args()
    api_key = os.getenv("API_KEY")
    if not api_key:
        sys.exit("API_KEY is not set. Source /etc/razzbot.env first — see the "
                 "docstring at the top of this file.")

    universe = os.getenv("DEV_UNIVERSE_ID") if args.dev else args.universe
    if not universe:
        sys.exit("--dev was passed but DEV_UNIVERSE_ID is not set.")

    entry = fetch(universe, args.datastore, args.key, api_key)
    value = entry.get("value")

    for field in ("etag", "revisionId", "revisionCreateTime", "createTime"):
        if entry.get(field):
            print(f"{field}: {entry[field]}")

    decoded, layers, raw_bytes = unwrap(value)

    print()
    if layers:
        field, marker = layers[0]
        print(f"VERDICT: stored as a Luau BUFFER "
              f"(t={marker!r}, payload in {field!r})")
        print("         the game can read this — leave it alone")
    else:
        print("VERDICT: stored as a PLAIN TABLE, not a buffer")
        print("         if the game expects a buffer here it will fail on")
        print("         buffer.tostring — repair with !leaderboard restore")

    if len(layers) > 1:
        print()
        print(f"WARNING: {len(layers)} buffer layers, one inside the next:")
        for depth, (field, marker) in enumerate(layers, 1):
            print(f"         {depth}. t={marker!r} payload in {field!r}")
        print("         the game unwraps one layer, so it sees the envelope")
        print("         below rather than the rows — this entry is damaged")
    elif layers:
        print(f"         (one layer, decoded from {layers[0][0]!r})")

    print()
    if raw_bytes is not None:
        print(f"contents: {len(raw_bytes)} bytes that are not JSON")
        print(raw_bytes[:200])
        return

    if isinstance(decoded, list):
        print(f"contents: list of {len(decoded)} row(s)")
        rows = decoded if args.full else decoded[:PREVIEW_ROWS]
        for position, row in enumerate(rows, 1):
            print(f"  {position:>3}. {json.dumps(row, ensure_ascii=False)}")
        if not args.full and len(decoded) > len(rows):
            print(f"  … {len(decoded) - len(rows)} more (pass --full)")
    else:
        print(f"contents: {type(decoded).__name__}")
        text = json.dumps(decoded, indent=2, ensure_ascii=False)
        print(text if args.full else text[:2000])


if __name__ == "__main__":
    main()
