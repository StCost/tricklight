"""TrickLight tray app (Windows): BLE control of a Trick-XXXX LED dimmer.

ON + saved brightness at launch (autostart at login), OFF at shutdown/logoff/sleep,
tray toggle, brightness presets and slider. Deps: bleak, pystray, pillow. Python 3.12.
Protocol (TrickLight app com.qh.onehlight 1.0.9): service ffd5 / char ffd9,
ON CC 23 33, OFF CC 24 33, brightness 56 VV 00 00 00 F0 AA (VV = pct*2.55), status EF 01 77.
"""
import asyncio, json, logging, logging.handlers, os, queue, sys, threading, time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
CFG_PATH = HERE / "tricklight.json"
LOG_PATH = HERE / "tricklight.log"
NAME_PREFIX = "Trick-"
DEFAULT_ADDR = "22:05:11:00:03:03"
CHR_WRITE = "0000ffd9-0000-1000-8000-00805f9b34fb"
CHR_NOTIFY = "0000ffd4-0000-1000-8000-00805f9b34fb"
CMD_ON, CMD_OFF, CMD_STATUS = b"\xCC\x23\x33", b"\xCC\x24\x33", b"\xEF\x01\x77"
PRESETS = (100, 75, 50, 25, 10, 5)
IS_WIN = sys.platform == "win32"

log = logging.getLogger("tricklight")


def setup_logging():
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    sys.excepthook = lambda *e: log.critical("uncaught", exc_info=e)
    threading.excepthook = lambda a: log.critical("uncaught in %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def bri_cmd(pct):
    v = int(max(0, min(100, int(pct))) * 2.55)
    return bytes([0x56, 255 if v >= 254 else v, 0, 0, 0, 0xF0, 0xAA])


class Config:
    def __init__(self, path=CFG_PATH):
        self.path, self.brightness, self.address = path, 10, DEFAULT_ADDR
        try:
            d = json.loads(path.read_text("utf-8"))
            self.brightness = max(1, min(100, int(d.get("brightness", 10))))
            self.address = d.get("address") or DEFAULT_ADDR
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("bad config, using defaults")

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"brightness": self.brightness, "address": self.address}, indent=1), "utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log.exception("config save failed")


class Light:
    """Owns one persistent BLE connection in a background asyncio thread. Thread-safe public API."""

    def __init__(self, cfg, on_change=lambda: None, client_cls=BleakClient, scanner_cls=BleakScanner):
        self.cfg, self.on_change = cfg, on_change
        self.client_cls, self.scanner_cls = client_cls, scanner_cls
        self.want_on, self.paused, self.stopping = True, False, False
        self.client, self.resp = None, False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._thread, name="ble", daemon=True)

    # ---- public (any thread) ----
    @property
    def connected(self):
        c = self.client
        return bool(c and c.is_connected)

    def start(self):
        self.thread.start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def set_power(self, on):
        self.want_on = on
        self.on_change()
        self.submit(self._power(on))

    def toggle(self):
        self.set_power(not self.want_on)

    def set_brightness(self, pct):
        pct = max(1, min(100, int(pct)))
        was_on, self.want_on = self.want_on, True
        self.cfg.brightness = pct
        self.cfg.save()
        self.on_change()
        self.submit(self._bri(pct, send_on=not was_on))

    def pause_off(self, timeout=3.0):
        """Shutdown/sleep: send OFF and block until done (keeps want_on so resume turns it back on)."""
        self.paused = True
        t0 = time.monotonic()
        try:
            self.submit(self._off_now(timeout - 0.3)).result(timeout)
            log.info("OFF sent before shutdown/sleep in %.2fs", time.monotonic() - t0)
            return True
        except Exception as e:
            log.warning("OFF before shutdown/sleep failed: %r", e)
            return False

    def resume(self):
        self.paused = False
        self.on_change()
        self.loop.call_soon_threadsafe(self._kick)

    def stop(self):
        self.stopping = True
        try:
            self.submit(self._disconnect()).result(3)
        except Exception:
            pass

    # ---- loop internals ----
    def _thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    def _kick(self):
        self.wake.set()

    async def _main(self):
        self.wake, self.lock = asyncio.Event(), asyncio.Lock()
        backoff = 2
        while not self.stopping:
            try:
                if not self.connected:
                    await self._connect()
                    backoff = 2
                    await self._apply()
                elif self.wake.is_set():
                    self.wake.clear()
                    await self._apply()
                try:
                    await asyncio.wait_for(self.wake.wait(), 20)
                except asyncio.TimeoutError:
                    pass
            except Exception as e:
                log.warning("BLE: %r (retry in %ss)", e, backoff)
                await self._disconnect()
                self.on_change()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _connect(self):
        dev = await self.scanner_cls.find_device_by_filter(
            lambda d, ad: (d.name or ad.local_name or "").startswith(NAME_PREFIX), timeout=6.0)
        if dev is not None:
            if dev.address != self.cfg.address:
                self.cfg.address = dev.address
                self.cfg.save()
            target = dev
        else:
            log.info("no %s* advert seen, trying cached %s", NAME_PREFIX, self.cfg.address)
            target = self.cfg.address
        self.wake.clear()
        c = self.client_cls(target, disconnected_callback=self._on_disc, timeout=15.0)
        await c.connect()
        self.client = c
        ch = c.services.get_characteristic(CHR_WRITE)
        if ch is None:
            raise RuntimeError("write characteristic ffd9 not found")
        self.resp = "write-without-response" not in ch.properties
        try:
            await c.start_notify(CHR_NOTIFY, self._on_notify)
        except Exception as e:
            log.info("notify unavailable: %r", e)
        log.info("connected to %s (write response=%s)", getattr(target, "address", target), self.resp)
        self.on_change()

    def _on_disc(self, _c):
        log.info("disconnected")
        self.loop.call_soon_threadsafe(self._kick)
        self.on_change()

    def _on_notify(self, _h, data):
        data = bytes(data)
        if len(data) > 2 and data[0] == 0x66:
            log.info("status %s power=%s", data.hex(" "), {0x23: "on", 0x24: "off"}.get(data[2], "?"))

    async def _disconnect(self):
        c, self.client = self.client, None
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _write(self, b):
        async with self.lock:
            if not self.connected:
                raise RuntimeError("not connected")
            await self.client.write_gatt_char(CHR_WRITE, b, response=self.resp)

    async def _apply(self):
        if self.paused or not self.connected:
            return
        if self.want_on:
            await self._write(CMD_ON)
            await asyncio.sleep(0.15)
            await self._write(bri_cmd(self.cfg.brightness))
            log.info("applied ON %d%%", self.cfg.brightness)
        else:
            await self._write(CMD_OFF)
            log.info("applied OFF")

    async def _safe(self, coro_fn):
        if self.paused:
            return
        if not self.connected:
            log.info("not connected; state will apply on reconnect")
            self._kick()
            return
        try:
            await coro_fn()
        except Exception as e:
            log.warning("write failed: %r", e)
            self._kick()

    async def _power(self, on):
        async def go():
            if on:
                await self._write(CMD_ON)
                await asyncio.sleep(0.15)
                await self._write(bri_cmd(self.cfg.brightness))
            else:
                await self._write(CMD_OFF)
                await asyncio.sleep(0.3)
                await self._write(CMD_OFF)
            log.info("power %s", "ON" if on else "OFF")
        await self._safe(go)

    async def _bri(self, pct, send_on):
        async def go():
            if send_on:
                await self._write(CMD_ON)
                await asyncio.sleep(0.15)
            await self._write(bri_cmd(pct))
            log.info("brightness %d%%", pct)
        await self._safe(go)

    async def _off_now(self, budget):
        end = time.monotonic() + max(0.5, budget)
        while not self.connected and time.monotonic() < end - 0.6:
            await asyncio.sleep(0.1)
        await self._write(CMD_OFF)
        await asyncio.sleep(0.3)
        await self._write(CMD_OFF)


# ---------------- tray icon image ----------------
def make_icon(on, connected, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    glass = (255, 205, 40, 255) if on else (150, 150, 150, 255)
    if not connected:
        glass = (90, 90, 90, 255)
    if on and connected:
        for x0, y0, x1, y1 in ((32, 1, 32, 7), (8, 10, 13, 15), (56, 10, 51, 15), (2, 30, 8, 30), (62, 30, 56, 30)):
            d.line([(x0 * s, y0 * s), (x1 * s, y1 * s)], fill=(255, 225, 90, 255), width=max(2, int(3 * s)))
    d.ellipse([14 * s, 9 * s, 50 * s, 45 * s], fill=glass, outline=(40, 40, 40, 255), width=max(1, int(2 * s)))
    d.rectangle([24 * s, 42 * s, 40 * s, 50 * s], fill=glass)
    d.rectangle([23 * s, 50 * s, 41 * s, 60 * s], fill=(110, 110, 120, 255))
    for y in (53, 57):
        d.line([(23 * s, y * s), (41 * s, y * s)], fill=(60, 60, 70, 255), width=max(1, int(s)))
    if not connected:
        d.line([(10 * s, 58 * s), (54 * s, 8 * s)], fill=(220, 50, 50, 255), width=max(2, int(5 * s)))
    return img


# ---------------- Windows glue (ctypes) ----------------
_mutex = None


def single_instance():
    global _mutex
    if not IS_WIN:
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = k32.CreateMutexW(None, False, "Local\\TrickLightTray_Mutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def start_session_watcher(light):
    """Hidden top-level window (thread) handling shutdown/logoff and sleep/resume."""
    import ctypes
    from ctypes import wintypes as W
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                    ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                    ("lpszClassName", W.LPCWSTR)]

    u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    u32.DefWindowProcW.restype = LRESULT
    u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u32.RegisterClassW.restype = W.ATOM
    u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
    u32.CreateWindowExW.restype = W.HWND
    u32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
    u32.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
    u32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
    u32.ShutdownBlockReasonCreate.argtypes = [W.HWND, W.LPCWSTR]
    u32.ShutdownBlockReasonDestroy.argtypes = [W.HWND]
    k32.GetModuleHandleW.argtypes = [W.LPCWSTR]
    k32.GetModuleHandleW.restype = W.HMODULE
    WM_QUERYENDSESSION, WM_ENDSESSION, WM_POWERBROADCAST = 0x11, 0x16, 0x218
    PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x4, 0x7, 0x12
    state = {"ended": False}

    def wndproc(hwnd, msg, wp, lp):
        try:
            if msg == WM_QUERYENDSESSION:
                log.info("WM_QUERYENDSESSION lParam=%#x", lp & 0xFFFFFFFF)
                if not state["ended"]:
                    state["ended"] = True
                    u32.ShutdownBlockReasonCreate(hwnd, "Turning off the light")
                    light.pause_off(3.0)
                    u32.ShutdownBlockReasonDestroy(hwnd)
                return 1
            if msg == WM_ENDSESSION:
                if wp:
                    log.info("session ending; exiting")
                    logging.shutdown()
                    os._exit(0)
                log.info("shutdown cancelled; turning light back on")
                state["ended"] = False
                light.resume()
                return 0
            if msg == WM_POWERBROADCAST:
                if wp == PBT_APMSUSPEND:
                    log.info("suspend")
                    light.pause_off(1.8)
                elif wp in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    if light.paused and not state["ended"]:
                        log.info("resume")
                        light.resume()
                return 1
        except Exception:
            log.exception("wndproc")
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def run():
        try:
            k32.SetProcessShutdownParameters(0x3FF, 0)  # ask to be notified early
        except Exception:
            pass
        proc = WNDPROC(wndproc)
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName="TrickLightSessionWnd")
        u32.RegisterClassW(ctypes.byref(wc))
        # top-level (NOT message-only) window, never shown: needed for broadcast messages
        hwnd = u32.CreateWindowExW(0, "TrickLightSessionWnd", "TrickLight", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            log.error("CreateWindowExW failed: %s", ctypes.get_last_error())
            return
        run.keep = (proc, wc)
        msg = W.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=run, name="session", daemon=True).start()


# ---------------- UI ----------------
class App:
    def __init__(self):
        import tkinter as tk
        import pystray
        self.tk, self.pystray = tk, pystray
        self.cfg = Config()
        self.light = Light(self.cfg, on_change=self.refresh)
        self.ui_q = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.slider = None
        M, I = pystray.Menu, pystray.MenuItem
        preset_items = [I(f"{p}%", self._preset(p), checked=self._is_preset(p), radio=True) for p in PRESETS]
        self.icon = pystray.Icon("TrickLight", make_icon(True, False), "TrickLight", menu=M(
            I("Toggle", lambda: self.light.toggle(), default=True, visible=False),
            I("Turn on", lambda: self.light.set_power(True)),
            I("Turn off", lambda: self.light.set_power(False)),
            I("Brightness", M(*preset_items)),
            I("Brightness…", lambda: self.ui_q.put(self.open_slider)),
            M.SEPARATOR,
            I("Exit", lambda: self.ui_q.put(self.exit)),
        ))

    def _preset(self, p):
        return lambda: self.light.set_brightness(p)

    def _is_preset(self, p):
        return lambda item: self.cfg.brightness == p

    def refresh(self):
        try:
            l = self.light
            on, conn = l.want_on and not l.paused, l.connected
            self.icon.icon = make_icon(on, conn)
            self.icon.title = f"TrickLight: {'ON' if on else 'OFF'} {self.cfg.brightness}%" + ("" if conn else " (not connected)")
            self.icon.update_menu()
        except Exception:
            pass

    def open_slider(self):
        tk = self.tk
        if self.slider is not None and self.slider.winfo_exists():
            self.slider.deiconify(); self.slider.lift(); self.slider.focus_force()
            return
        w = self.slider = tk.Toplevel(self.root)
        w.title("TrickLight brightness")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        var = tk.IntVar(value=self.cfg.brightness)
        lbl = tk.Label(w, text=f"{var.get()}%", font=("Segoe UI", 14))
        lbl.pack(padx=12, pady=(10, 0))
        sc = tk.Scale(w, from_=1, to=100, orient="horizontal", length=280, variable=var, showvalue=False,
                      command=lambda v: lbl.config(text=f"{int(float(v))}%"))
        sc.pack(padx=12, pady=(0, 12))
        pending = {"id": None}

        def apply_later(_e=None):
            if pending["id"]:
                w.after_cancel(pending["id"])
            pending["id"] = w.after(200, lambda: self.light.set_brightness(var.get()))

        for ev in ("<ButtonRelease-1>", "<KeyRelease>"):
            sc.bind(ev, apply_later)
        sc.bind("<MouseWheel>", lambda e: (var.set(max(1, min(100, var.get() + (1 if e.delta > 0 else -1)))),
                                           lbl.config(text=f"{var.get()}%"), apply_later()))
        w.bind("<Escape>", lambda e: w.destroy())
        w.update_idletasks()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"+{sw - w.winfo_width() - 24}+{sh - w.winfo_height() - 90}")
        sc.focus_set()

    def _poll(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def exit(self):  # does NOT turn the light off
        log.info("exit requested")
        self.icon.stop()
        self.light.stop()
        self.root.quit()

    def run(self):
        self.light.start()
        if IS_WIN:
            start_session_watcher(self.light)
        self.icon.run_detached()
        self.icon.visible = True
        self.refresh()
        self.root.after(100, self._poll)
        self.root.mainloop()


def main():
    setup_logging()
    if not single_instance():
        log.info("already running; exiting")
        return
    log.info("starting (python %s)", sys.version.split()[0])
    App().run()
    log.info("stopped")


if __name__ == "__main__":
    main()
