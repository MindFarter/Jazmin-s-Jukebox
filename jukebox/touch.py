"""
CST816D capacitive touch controller driver for Jazmin's JukeBox.

The 2-inch Pi Hut / Waveshare panel's touch controller is a CST816D on
I2C bus 1 at address 0x15. This driver polls the chip (like the
vendor's own Raspberry Pi demo), so no kernel module or interrupt
wiring is required - only TP_SDA/TP_SCL need to be connected.

Register map (from the CST816D datasheet):
    0x01 gesture id
    0x02 number of touch points
    0x03 X_H (event flags in high nibble)
    0x04 X_L
    0x05 Y_H
    0x06 Y_L

Gesture ids: 0x01 slide down, 0x02 slide up, 0x03 slide left,
0x04 slide right, 0x05 single tap, 0x0B double tap, 0x0C long press.
"""

import threading
import time

try:
    import smbus2
except ImportError:  # allows simulation off-Pi
    smbus2 = None


GESTURES = {
    0x01: "swipe_down",
    0x02: "swipe_up",
    0x03: "swipe_left",
    0x04: "swipe_right",
    0x05: "tap",
    0x0B: "double_tap",
    0x0C: "long_press",
}


class TouchEvent:
    __slots__ = ("kind", "x", "y")

    def __init__(self, kind, x=None, y=None):
        self.kind = kind      # "tap", "double_tap", "swipe_*", "long_press"
        self.x = x
        self.y = y

    def __repr__(self):
        return f"<TouchEvent {self.kind} ({self.x},{self.y})>"


class CST816D:
    """Polled CST816D touch driver with a background reader thread."""

    I2C_ADDR = 0x15

    def __init__(self, i2c_bus=1, poll_hz=30.0, width=240, height=320,
                 swap_xy=False, flip_x=False, flip_y=False):
        self.width = width
        self.height = height
        self.swap_xy = swap_xy
        self.flip_x = flip_x
        self.flip_y = flip_y
        self._queue = []
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._last_touch_time = 0.0

        if smbus2 is not None:
            self._bus = smbus2.SMBus(i2c_bus)
        else:
            self._bus = None

        self._down = False
        self._down_x = 0
        self._down_y = 0
        self._down_t = 0.0
        self._last_tap_t = 0.0

    # -- raw read ------------------------------------------------------------
    def _read_raw(self):
        if self._bus is None:
            return None
        try:
            data = self._bus.read_i2c_block_data(self.I2C_ADDR, 0x01, 6)
        except OSError:
            return None
        gesture = data[0]
        points = data[1] & 0x0F
        x = ((data[2] & 0x0F) << 8) | data[3]
        y = ((data[4] & 0x0F) << 8) | data[5]
        return gesture, points, x, y

    def _transform(self, x, y):
        if self.swap_xy:
            x, y = y, x
        if self.flip_x:
            x = self.width - 1 - x
        if self.flip_y:
            y = self.height - 1 - y
        return x, y

    # -- event generation ----------------------------------------------------
    def _process(self):
        raw = self._read_raw()
        if raw is None:
            return
        gesture, points, x, y = raw
        now = time.monotonic()
        self._last_touch_time = now

        if points >= 1:
            if not self._down:
                self._down = True
                self._down_x, self._down_y = self._transform(x, y)
                self._down_t = now
        else:
            if self._down:
                self._down = False
                dur = now - self._down_t
                if dur > 0.7:
                    self._emit(TouchEvent("long_press", self._down_x, self._down_y))
                    return
                if now - self._last_tap_t < 0.30:
                    self._emit(TouchEvent("double_tap", self._down_x, self._down_y))
                    self._last_tap_t = 0.0
                else:
                    self._emit(TouchEvent("tap", self._down_x, self._down_y))
                    self._last_tap_t = now

        # gesture ids reported by the chip (swipes etc.)
        if gesture in GESTURES and GESTURES[gesture] not in ("tap",):
            self._emit(TouchEvent(GESTURES[gesture]))

    def _emit(self, event):
        with self._lock:
            self._queue.append(event)
            # keep the queue bounded
            del self._queue[:-32]

    # -- public API ----------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        interval = 1.0 / self.poll_hz if self.poll_hz else 0.03
        while self._running:
            try:
                self._process()
            except Exception:
                pass
            time.sleep(interval)

    def stop(self):
        self._running = False
        self._thread = None

    def get_events(self):
        """Return and clear queued touch events."""
        with self._lock:
            events = self._queue
            self._queue = []
        return events

    def seconds_since_touch(self):
        return time.monotonic() - self._last_touch_time
