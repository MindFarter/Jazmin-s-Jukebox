"""
ST7789 / ST7789T3 SPI display driver for Jazmin's JukeBox.

Drives the 2-inch 240x320 IPS panel (Waveshare-style ST7789T3) found on
the Pi Hut "2 Capacitive Touch IPS LCD Display" over 4-wire SPI.

This is a self-contained driver: it needs only spidev, RPi.GPIO and
numpy/Pillow. It intentionally does NOT use the Linux framebuffer, so the
JukeBox UI owns the whole screen without X11/Wayland.

Pin defaults match docs/HARDWARE.md and jukebox/config.json:
    LCD_CS  -> GPIO 8  (SPI CE0)
    LCD_DC  -> GPIO 17
    LCD_RST -> GPIO 4
    LCD_BL  -> GPIO 12 (hardware PWM capable, used for dimming)
"""

import time

try:
    import spidev
except ImportError:  # allows UI development/simulation off-Pi
    spidev = None

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover
    GPIO = None

try:
    import numpy as np
except ImportError:
    np = None


# --- ST7789 commands -------------------------------------------------------
SWRESET = 0x01
SLPOUT = 0x11
NORON = 0x13
INVOFF = 0x20
INVON = 0x21
DISPOFF = 0x28
DISPON = 0x29
CASET = 0x2A
RASET = 0x2B
RAMWR = 0x2C
MADCTL = 0x36
COLMOD = 0x3A

_MADCTL_MY = 0x80
_MADCTL_MX = 0x40
_MADCTL_MV = 0x20


class ST7789:
    """240x320 portrait ST7789 panel on SPI."""

    def __init__(self, width=240, height=320, rotation=0,
                 spi_bus=0, spi_device=0, spi_speed_hz=40_000_000,
                 pin_dc=17, pin_rst=4, pin_bl=12,
                 gpio_mode_bcm=True):
        self.width = width
        self.height = height
        self.rotation = ((rotation % 360) // 90) * 90
        self.pin_dc = pin_dc
        self.pin_rst = pin_rst
        self.pin_bl = pin_bl

        self._gpio_ok = GPIO is not None and spidev is not None
        if self._gpio_ok:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self.pin_dc, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(self.pin_rst, GPIO.OUT, initial=GPIO.HIGH)
            GPIO.setup(self.pin_bl, GPIO.OUT, initial=GPIO.HIGH)

            self._spi = spidev.SpiDev()
            self._spi.open(spi_bus, spi_device)
            self._spi.max_speed_hz = spi_speed_hz
            self._spi.mode = 0
            self._spi.lsbfirst = False
        else:
            self._spi = None  # simulation mode

        self._init_sequence()
        self.set_backlight(True)

    # -- low level ----------------------------------------------------------
    def _command(self, cmd, data=b""):
        if self._spi is None:
            return
        if self._gpio_ok:
            GPIO.output(self.pin_dc, GPIO.LOW)
        self._spi.writebytes([cmd])
        if data:
            if self._gpio_ok:
                GPIO.output(self.pin_dc, GPIO.HIGH)
            self._spi.writebytes(list(data))

    def _reset(self):
        if not self._gpio_ok:
            return
        GPIO.output(self.pin_rst, GPIO.HIGH)
        time.sleep(0.02)
        GPIO.output(self.pin_rst, GPIO.LOW)
        time.sleep(0.002)
        GPIO.output(self.pin_rst, GPIO.HIGH)
        time.sleep(0.02)

    def _init_sequence(self):
        self._reset()
        self._command(SWRESET)
        time.sleep(0.15)
        self._command(SLPOUT)            # exit sleep
        time.sleep(0.12)

        # rotation / mirror
        madctl = 0x00
        r = self.rotation
        if r == 90:
            madctl = _MADCTL_MV | _MADCTL_MX
        elif r == 180:
            madctl = _MADCTL_MX | _MADCTL_MY
        elif r == 270:
            madctl = _MADCTL_MV | _MADCTL_MY
        self._command(MADCTL, [madctl])

        self._command(COLMOD, [0x55])   # 16 bits per pixel
        self._command(0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33])  # porch
        self._command(0xB7, [0x35])     # gate control
        self._command(0xBB, [0x19])     # VCOM
        self._command(0xC0, [0x2C])     # LCMCTRL
        self._command(0xC2, [0x01, 0xC3])
        self._command(0xC3, [0x12])     # VRHS
        self._command(0xC4, [0x20])     # VDV
        self._command(0xC6, [0x0F])     # frctrl2 (60Hz)
        self._command(0xD0, [0xA4, 0xA1])  # power control
        # gamma (positive / negative)
        self._command(0xE0, [0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B,
                            0x3F, 0x54, 0x4C, 0x18, 0x0D, 0x0B,
                            0x1F, 0x23])
        self._command(0xE1, [0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B,
                            0x3F, 0x54, 0x4C, 0x18, 0x0D, 0x0B,
                            0x1F, 0x23])

        self._command(INVON)             # IPS panels are inverted
        self._command(SLPOUT)
        time.sleep(0.12)
        self._command(DISPON)

    # -- public API ----------------------------------------------------------
    def set_backlight(self, on):
        if self._gpio_ok:
            GPIO.output(self.pin_bl, GPIO.HIGH if on else GPIO.LOW)

    def _set_window(self, x0, y0, x1, y1):
        # account for rotation so callers always use portrait coordinates
        w, h = self._rotated_size()
        if self.rotation in (90, 270):
            x0, y0, x1, y1 = y0, x0, y1, x1
        self._command(CASET, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self._command(RASET, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])

    def _rotated_size(self):
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @staticmethod
    def _rgb565_bytes(image):
        """Convert an RGB PIL image to big-endian RGB565 bytes."""
        img = image.convert("RGB")
        if np is not None:
            a = np.asarray(img, dtype=np.uint16)
            out = ((a[:, :, 0] & 0xF8) << 8) | ((a[:, :, 1] & 0xFC) << 3) | (a[:, :, 2] >> 3)
            return out.astype(">u2").tobytes()
        # pure-python fallback
        r, g, b = img.split()
        rb, gb, bb = r.tobytes(), g.tobytes(), b.tobytes()
        buf = bytearray(len(rb) * 2)
        i = 0
        for r8, g8, b8 in zip(rb, gb, bb):
            v = ((r8 >> 3) << 11) | ((g8 >> 2) << 5) | (b8 >> 3)
            buf[i] = v >> 8
            buf[i + 1] = v & 0xFF
            i += 2
        return bytes(buf)

    def blit(self, image, x=0, y=0):
        """Draw a PIL image at (x, y) in portrait coordinates."""
        w, h = self._rotated_size()
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))
        cw = min(image.width, w - x)
        ch = min(image.height, h - y)
        if cw <= 0 or ch <= 0:
            return
        if (cw, ch) != image.size:
            image = image.crop((0, 0, cw, ch))
        # if rotated, rotate the image so it lands correctly
        if self.rotation == 90:
            image = image.rotate(-90, expand=True)
        elif self.rotation == 180:
            image = image.rotate(180, expand=True)
        elif self.rotation == 270:
            image = image.rotate(90, expand=True)
        self._set_window(x, y, x + image.width - 1, y + image.height - 1)
        self._command(RAMWR)
        if self._spi is None:
            return
        if self._gpio_ok:
            GPIO.output(self.pin_dc, GPIO.HIGH)
        data = self._rgb565_bytes(image)
        # write in chunks (spidev limits)
        chunk = 4096
        for i in range(0, len(data), chunk):
            self._spi.writebytes2(data[i:i + chunk])

    def clear(self, color=(0, 0, 0)):
        from PIL import Image
        self.blit(Image.new("RGB", self._rotated_size(), color))

    def close(self):
        self.set_backlight(False)
        try:
            if self._spi is not None:
                self._spi.close()
        except Exception:
            pass
        if self._gpio_ok:
            try:
                GPIO.cleanup()
            except Exception:
                pass
