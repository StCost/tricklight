#!/usr/bin/env python3
"""TrickLight (Trick-XXXX) BLE LED controller - proof of concept.

Protocol reverse-engineered from TrickLight Android app com.qh.onehlight v1.0.9
(com/qh/blelight/MyBluetoothGatt.java, BluetoothLeService.java, com/qh/tools/DeviceUUID.java).
Triones-family protocol.

    pip install bleak
    python tricklight.py scan
    python tricklight.py on|off|status [--addr AA:BB:..]
    python tricklight.py brightness 10 [--addr ...]
"""
import argparse, asyncio, re, sys
from bleak import BleakClient, BleakScanner

NAME_RE = re.compile(r"^Trick-|^Trick#")          # BluetoothLeService.COMPANY_NAME
SVC_WRITE = "0000ffd5-0000-1000-8000-00805f9b34fb"  # CONSMART_..._SERVICE_WRGB_UUID
CHR_WRITE = "0000ffd9-0000-1000-8000-00805f9b34fb"  # CONSMART_..._CHARACTERISTICS_WRGB_UUID
CHR_NOTIFY = "0000ffd4-0000-1000-8000-00805f9b34fb" # in service ffd0, status replies

CMD_ON = bytes([0xCC, 0x23, 0x33])                  # openLight(true)
CMD_OFF = bytes([0xCC, 0x24, 0x33])                 # openLight(false), app sends twice 300 ms apart
CMD_STATUS = bytes([0xEF, 0x01, 0x77])              # getLightData()


def cmd_brightness(pct: int) -> bytes:
    """setColor(int): pct 0..100 -> v = int(pct*2.55), >=254 -> 255; 56 v 00 00 00 F0 AA."""
    pct = max(0, min(100, int(pct)))
    v = int(pct * 2.55)
    if v >= 254:
        v = 255
    return bytes([0x56, v, 0x00, 0x00, 0x00, 0xF0, 0xAA])


async def find(addr=None, timeout=8.0):
    if addr:
        return addr
    devs = await BleakScanner.discover(timeout=timeout)
    for d in devs:
        if d.name and NAME_RE.search(d.name):
            print(f"found {d.name} {d.address}", file=sys.stderr)
            return d.address
    raise SystemExit("No Trick-* device found (close the phone app, it holds the connection)")


async def scan(timeout):
    for d in await BleakScanner.discover(timeout=timeout):
        if d.name and NAME_RE.search(d.name):
            print(d.address, d.name)


async def run(action, addr, value=None):
    address = await find(addr)
    async with BleakClient(address, timeout=15.0) as c:
        replies = []
        try:
            await c.start_notify(CHR_NOTIFY, lambda _h, data: replies.append(bytes(data)))
        except Exception as e:  # not essential for control
            print(f"notify not enabled: {e}", file=sys.stderr)

        async def w(b):
            # App uses write-with-response for most cmds, no-response for on/off.
            # Firmware accepts both; fall back if one is unsupported.
            try:
                await c.write_gatt_char(CHR_WRITE, b, response=False)
            except Exception:
                await c.write_gatt_char(CHR_WRITE, b, response=True)

        if action == "on":
            await w(CMD_ON)
        elif action == "off":
            await w(CMD_OFF); await asyncio.sleep(0.3); await w(CMD_OFF)
        elif action == "brightness":
            await w(cmd_brightness(value))
            await asyncio.sleep(0.3); await w(cmd_brightness(value))  # app re-sends after 300 ms
        elif action == "status":
            await w(CMD_STATUS)
            await asyncio.sleep(1.5)
            for r in replies:
                print(r.hex(" "))
                if r and r[0] == 0x66 and len(r) > 2:
                    print("power:", {0x23: "ON", 0x24: "OFF"}.get(r[2], hex(r[2])))
                    if len(r) >= 12:
                        print("R/brightness byte:", r[6], "mode:", hex(r[3]))
        await asyncio.sleep(0.3)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["scan", "on", "off", "brightness", "status"])
    p.add_argument("value", nargs="?", type=int, default=10, help="brightness percent (default 10)")
    p.add_argument("--addr", help="BLE address, skips scanning")
    p.add_argument("--timeout", type=float, default=8.0)
    a = p.parse_args()
    if a.action == "scan":
        asyncio.run(scan(a.timeout))
    else:
        asyncio.run(run(a.action, a.addr, a.value))


if __name__ == "__main__":
    main()
