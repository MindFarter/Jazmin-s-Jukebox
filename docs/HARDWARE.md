# Hardware guide — Jazmin's JukeBox

## The system

```
                 Wi-Fi ──────┐
   Phone (Iris UI)          ▼
                      Pi Zero 2 W
                     ┌───────────────┐
   2" touch screen ───┤ SPI + I2C     │
   (ST7789 + CST816D) │               │
                     │  I2S + I2C    │
                     └──────┬────────┘
                            │
                      IQaudio DigiAMP+
                     (12–24 V DC in) ──── 12 V battery / AC-DC transformer
                            │
                    ┌───────┴───────┐
                    │               │
                Speaker L       Speaker R
                (15 W 4 Ω)     (15 W 4 Ω)
```

## Display wiring (15-pin GH1.25 cable)

The Pi Hut 2" display module pins map to the Pi Zero 2 W header like this:

| Module pin | Signal  | Pi header pin | Pi GPIO |
|---|---|---|---|
| 1  | VCC     | 2  (5V)  | — |
| 2  | 3V3     | 1  (3.3V)| — |
| 3  | GND     | 6  (GND) | — |
| 4  | MISO    | 21       | GPIO 9  |
| 5  | MOSI    | 19       | GPIO 10 |
| 6  | SCLK    | 23       | GPIO 11 |
| 7  | SD_CS   | 22       | GPIO 7 (CE1) |
| 8  | LCD_CS  | 24       | GPIO 8 (CE0) |
| 9  | LCD_DC  | 11       | GPIO 17 |
| 10 | LCD_RST | 7        | GPIO 4 |
| 11 | LCD_BL  | 32       | GPIO 12 |
| 12 | TP_SDA  | 3        | GPIO 2 (I2C SDA) |
| 13 | TP_SCL  | 5        | GPIO 3 (I2C SCL) |
| 14 | TP_INT  | not connected (we poll) | — |
| 15 | TP_RST  | not connected (or 3.3V) | — |

**The SD card slot on the screen is where the MP3s live.** The module's TF slot shares the SPI bus with the LCD, using its own chip select (SD_CS → GPIO 7 / CE1). The installer compiles a small device-tree overlay (`overlays/jazmin-sd.dts`) that binds the kernel's `mmc-spi-slot` driver to that chip select, so the card shows up as a normal disk and is mounted at **/mnt/screen-sd** — that is the JukeBox's music library. It is slower than a USB stick (~1 MB/s over SPI) but far more than enough for playing MP3s. Put MP3/WAV/FLAC files on the card (FAT32) from any computer, then run `sudo jukebox-scan`.

Notes:
- The card must be inserted at boot, or inserted later and `sudo systemctl restart mopidy` run after mounting.
- If you ever want to remove this feature, delete `dtoverlay=jazmin-sd` from `/boot/firmware/config.txt`.
- A USB stick (via a micro-USB OTG adapter) also works as additional storage — add it to `media_dirs` in `/etc/mopidy/mopidy.conf`.

**GPIO conflicts — important.** The DigiAMP+ occupies GPIO 18–27 (I2S + amp control). That is why the wiring above uses GPIO 4/7/8/9/10/11/17 for the display and screen SD. Do not move LCD_DC/RST/BL or SD_CS onto GPIO 18–27 or the amp and screen will fight.

**Touch needs only SDA/SCL** (pins 12/13 on the module cable). The UI polls the CST816D over I2C at 0x15, so TP_INT/TP_RST can stay unconnected.

## DigiAMP+ wiring

- Power the DigiAMP+ from its own **12–24 V DC input** (centre-positive). Your 12 V battery or the correctly rated AC/DC transformer both work; the DigiAMP+ in turn powers the Pi over the GPIO header, so no separate USB supply is needed. **Do not also connect power to the Pi's USB power input while using the DigiAMP+.**
- Speakers connect to the DigiAMP+ screw terminals — **observe polarity** (+/−) on both channels.
- The amp handles 4 Ω loads fine; your 15 W speakers are well within its capability. Start with the volume low.
- A 12 V supply is within the DigiAMP+'s 12–24 V input range. Use a supply rated for the amplifier and the connected hardware; the speaker's 15 W rating is not a limiter, so keep the volume at a level the speakers can safely handle.
- The UI reads the Raspberry Pi firmware's current undervoltage flag and displays `LOW` if the Pi's 5 V supply is below its safe threshold, or `OK` when no warning is active. Because the DigiAMP+ converts the battery input to power the Pi, a low battery may eventually trigger this warning; it can also be caused by supply, wiring, or load problems. This is not a direct 12 V battery voltage/current reading; use your separate battery volt/amp meter for those measurements.
- Audio output is configured by `install.sh` via `dtoverlay=iqaudio-dacplus,auto_mute_amp` (the amp mutes itself when nothing plays — good for battery use). If you prefer it always on, change it to `unmute_amp` in `/boot/firmware/config.txt`.

## Pi Zero 2 W + DigiAMP+ physical note

The DigiAMP+ is a full-size HAT. On a Zero 2 W you need a GPIO adapter cable/header expander to connect both the DigiAMP+ and the display's wires. Any standard "Pi Zero to HAT" adapter cable works; keep the wiring table above for the display side.

## First-boot checklist

1. DigiAMP+ has 12 V; Pi LED lights up.
2. Boot; wait ~1 minute. The screen shows the Jazmin's JukeBox header and "Starting up…".
3. `ssh pi@jazmin.local` works, Iris loads on your phone.
4. `sudo i2cdetect -y 1` shows `15` (touch controller present).
5. Play a preset; music comes out of both speakers.

If the screen is blank but everything else works, re-check pins 8/9/10 of the module cable (CS/DC/RST) — they are the usual suspects.
