#!/usr/bin/env python3
"""
NetControl Online — APRS relay script

Connects to an APRS-IS feed — either the public APRS-IS network (online
mode, e.g. rotate.aprs2.net:14580) or a local Direwolf/TNC/igate on your
LAN (offline/local-hardware mode) — and forwards parsed station positions
to the NetControl Online backend. Same script, same flags; only --host/
--port change between the two scenarios described in issue #22.

All TNC2 parsing happens here (that's inherently relay-script territory);
the backend receives already-normalized entries via a single push endpoint.

Usage
-----
    python3 aprs_relay.py [options]

Required options (or set matching environment variables):
    --server        NetControl Online base URL, e.g. https://tracker.netcontrol.online
    --token         API token (create one under Account -> API Tokens)
    --net-id        Net ID to push data to (shown in the net's URL)
    --my-callsign   Your callsign, used to log into APRS-IS

Optional:
    --host          APRS-IS server (default: rotate.aprs2.net)
    --port          APRS-IS port (default: 14580)
    --callsigns     Comma-separated watch-list, e.g. W1AW,K1ABC-9
                    Builds a server-side buddy filter so the feed isn't a
                    firehose. If omitted, everything the server sends is
                    parsed (only useful with a small local igate feed).
    --interval      How often to push batched entries, in seconds (default: 30)

Environment variables
---------------------
    NT_SERVER, NT_TOKEN, NT_NET_ID, NT_MY_CALLSIGN, NT_HOST, NT_PORT,
    NT_CALLSIGNS, NT_INTERVAL

Example (public APRS-IS network — "online mode" in issue #22)
---------------------------------------------------------------
    python3 aprs_relay.py \\
        --server https://tracker.netcontrol.online \\
        --token nt_abc123... \\
        --net-id 1 \\
        --my-callsign W1AW \\
        --callsigns W1AW-9,K1ABC-9,N1XYZ-9

Example (local Direwolf/TNC/igate — "offline mode" in issue #22)
-------------------------------------------------------------------
    python3 aprs_relay.py \\
        --server http://192.168.1.50:8000 \\
        --token nt_abc123... \\
        --net-id 1 \\
        --my-callsign W1AW \\
        --host 192.168.1.10 --port 8001

Notes on parsing (v1 scope)
----------------------------
Only standard *uncompressed* position packets (data type identifiers
"!", "=", "/", "@") are parsed. Compressed position format and Mic-E
decoding are not handled in this version — a station using either will
simply not appear on the map until a future update adds support.
"""

import argparse
import os
import re
import sys
import time
import socket
import logging
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("requests not installed — run: pip install requests")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [aprs-relay] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("aprs_relay")


# ── APRS-IS passcode algorithm ──
# Well-known, publicly-documented checksum-style algorithm used to log
# into APRS-IS. Verified against the published reference: N0CALL -> 13023.
def aprs_passcode(callsign: str) -> int:
    """Compute the APRS-IS login passcode for a callsign (SSID is ignored)."""
    callsign = callsign.split("-")[0].upper()
    code = 0x73E2
    for i, ch in enumerate(callsign):
        if i % 2 == 0:
            code ^= ord(ch) << 8
        else:
            code ^= ord(ch)
    return code & 0x7FFF


# ── TNC2 uncompressed position parsing ──
def _parse_aprs_timestamp(ts: str) -> str | None:
    """Parse a 7-char APRS timestamp (DDHHMMz or HHMMSSh) into a UTC string.

    Day-of-month timestamps (the common "z" form) assume the current
    UTC month/year, which is correct except right at a month boundary —
    an accepted v1 limitation since APRS-IS timestamps are only ever a
    few seconds old in practice.
    """
    if len(ts) != 7:
        return None
    kind = ts[6]
    now = datetime.now(timezone.utc)
    try:
        if kind in ("z", "/"):
            day, hour, minute = int(ts[0:2]), int(ts[2:4]), int(ts[4:6])
            dt = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        elif kind == "h":
            hour, minute, second = int(ts[0:2]), int(ts[2:4]), int(ts[4:6])
            dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        else:
            return None
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return None


def _parse_latlon(lat_str: str, lon_str: str) -> tuple[float, float]:
    """Decode DDMM.hhN / DDDMM.hhW fields into signed decimal degrees."""
    lat_deg = int(lat_str[0:2])
    lat_min = float(lat_str[2:7])
    lat = lat_deg + lat_min / 60.0
    if lat_str[7] == "S":
        lat = -lat

    lon_deg = int(lon_str[0:3])
    lon_min = float(lon_str[3:8])
    lon = lon_deg + lon_min / 60.0
    if lon_str[8] == "W":
        lon = -lon

    return lat, lon


_COURSE_SPEED_RE = re.compile(r"^(\d{3})/(\d{3})")
_ALTITUDE_RE = re.compile(r"/A=(-?\d{6})")


def parse_tnc2_line(line: str) -> dict | None:
    """Parse one TNC2-format APRS-IS line into a position entry dict, or
    None if it isn't a station position packet we understand (v1 only
    handles uncompressed lat/lon position formats)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if ">" not in line or ":" not in line:
        return None

    header, _, info = line.partition(":")
    callsign = header.split(">", 1)[0].strip()
    if not callsign or not info:
        return None

    dtype = info[0]
    if dtype not in "!=/@":
        return None

    try:
        body = info[1:]
        heard_at = None
        if dtype in "/@":
            heard_at = _parse_aprs_timestamp(body[0:7])
            body = body[7:]

        if len(body) < 19:
            return None
        # Compressed position packets start with a symbol-table char that
        # is not a digit and the fixed-width fields below won't parse
        # cleanly — bail out rather than emit garbage coordinates.
        if not body[0].isdigit():
            return None

        lat_str = body[0:8]
        lon_str = body[9:18]
        symbol = body[8] + body[18]
        comment = body[19:]

        lat, lon = _parse_latlon(lat_str, lon_str)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None

        entry = {"callsign": callsign, "lat": lat, "lon": lon, "symbol": symbol}
        if heard_at:
            entry["heard_at"] = heard_at

        m = _COURSE_SPEED_RE.match(comment)
        if m:
            entry["course"] = int(m.group(1))
            entry["speed"] = float(int(m.group(2)))  # knots
            comment = comment[7:]

        alt_m = _ALTITUDE_RE.search(comment)
        if alt_m:
            entry["altitude"] = float(int(alt_m.group(1)))  # feet
            comment = comment[: alt_m.start()] + comment[alt_m.end():]

        comment = comment.strip()
        if comment:
            entry["comment"] = comment

        return entry
    except (ValueError, IndexError):
        return None


def parse_args():
    p = argparse.ArgumentParser(description="NetControl Online APRS relay script")
    p.add_argument("--server",      default=os.getenv("NT_SERVER", ""))
    p.add_argument("--token",       default=os.getenv("NT_TOKEN", ""))
    p.add_argument("--net-id",      default=os.getenv("NT_NET_ID", ""), type=int)
    p.add_argument("--my-callsign", default=os.getenv("NT_MY_CALLSIGN", ""))
    p.add_argument("--host",        default=os.getenv("NT_HOST", "rotate.aprs2.net"))
    p.add_argument("--port",        default=int(os.getenv("NT_PORT", "14580")), type=int)
    p.add_argument("--callsigns",   default=os.getenv("NT_CALLSIGNS", ""))
    p.add_argument("--interval",    default=int(os.getenv("NT_INTERVAL", "30")), type=int)
    args = p.parse_args()

    missing = [f"--{k}" for k, v in {
        "server": args.server,
        "token": args.token,
        "net-id": args.net_id,
        "my-callsign": args.my_callsign,
    }.items() if not v]
    if missing:
        p.error(f"Missing required options: {', '.join(missing)}")

    return args


def push_entries(server: str, token: str, net_id: int, entries: list[dict]) -> bool:
    """POST already-normalized entries to /nets/{id}/aprs/push. Returns True on success."""
    url = f"{server.rstrip('/')}/nets/{net_id}/aprs/push"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"entries": entries}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 204:
            return True
        log.warning("Push failed: HTTP %s — %s", r.status_code, r.text[:200])
        return False
    except Exception as exc:
        log.warning("Push error: %s", exc)
        return False


def connect_aprs_is(host: str, port: int, my_callsign: str, callsigns: str) -> socket.socket:
    """Open the APRS-IS TCP connection and send the login line."""
    sock = socket.create_connection((host, port), timeout=30)
    sock.settimeout(60)

    passcode = aprs_passcode(my_callsign)
    login = f"user {my_callsign} pass {passcode} vers NetControlOnline-APRS-Relay 1.0"
    if callsigns.strip():
        buddies = "/".join(c.strip().upper() for c in callsigns.split(",") if c.strip())
        login += f" filter b/{buddies}"
    login += "\r\n"

    sock.sendall(login.encode("ascii", errors="ignore"))
    log.info("Connected to %s:%s as %s", host, port, my_callsign)
    return sock


def main():
    args = parse_args()
    log.info(
        "Starting APRS relay: net=%s host=%s:%s callsigns=%s interval=%ss",
        args.net_id, args.host, args.port, args.callsigns or "(all)", args.interval,
    )

    consecutive_errors = 0
    while True:
        sock = None
        try:
            sock = connect_aprs_is(args.host, args.port, args.my_callsign, args.callsigns)
            buf = b""
            last_push = time.time()
            pending: dict[str, dict] = {}  # keyed by callsign — latest position wins

            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    chunk = b""
                if chunk:
                    buf += chunk
                    while b"\r\n" in buf or b"\n" in buf:
                        line, sep, buf = buf.partition(b"\n" if b"\n" in buf else b"\r\n")
                        try:
                            text = line.decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                        entry = parse_tnc2_line(text)
                        if entry:
                            pending[entry["callsign"]] = entry
                    consecutive_errors = 0

                if time.time() - last_push >= args.interval:
                    if pending:
                        entries = list(pending.values())
                        ok = push_entries(args.server, args.token, args.net_id, entries)
                        if ok:
                            log.info("Pushed %d station(s)", len(entries))
                            pending.clear()
                        else:
                            consecutive_errors += 1
                    last_push = time.time()

        except Exception as exc:
            consecutive_errors += 1
            log.error("Relay error (%d consecutive): %s", consecutive_errors, exc)
            if consecutive_errors >= 10:
                log.error("Too many consecutive errors — check network/hardware connectivity")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        log.info("Reconnecting in %ss...", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
