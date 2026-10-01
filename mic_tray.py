"""
mic_tray.py — Toggle de micrófono en la barra de tareas de Windows.

Widget "circular" (transparentcolor) con disco gris y, en el centro, un
icono de micrófono que se transforma con crossfade en un mini ecualizador
de 3 barras verdes cuando detecta voz (con histéresis para no titilar en
el umbral). Muteado: el icono de mic queda fijo en rojo, sin animación
(no hay stream abierto mientras está muteado).

Render con Pillow + supersampling 2x para antialiasing.

Requiere: pip install -r requirements.txt
"""

import atexit
import ctypes
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes

import numpy as np
import sounddevice as sd
from PIL import Image, ImageDraw, ImageTk
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# ── Hotkey global: Ctrl+Shift+F12 ──────────────────────────────
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
VK_F12 = 0x7B
HOTKEY_ID = 1

# ── Single-instance: kill + replace via lockfile con PID ────────
_LOCK_DIR = os.path.join(tempfile.gettempdir(), "MicToggle")
_LOCKFILE = os.path.join(_LOCK_DIR, "mic_tray.pid")


def _enforce_single_instance():
    os.makedirs(_LOCK_DIR, exist_ok=True)
    if os.path.exists(_LOCKFILE):
        try:
            with open(_LOCKFILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if old_pid and old_pid != os.getpid():
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(old_pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                time.sleep(0.3)
        except (ValueError, OSError):
            pass
    with open(_LOCKFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(_cleanup_lockfile)


def _cleanup_lockfile():
    try:
        if os.path.exists(_LOCKFILE):
            with open(_LOCKFILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(_LOCKFILE)
    except Exception:
        pass


_enforce_single_instance()


def get_mic_volume():
    devices = AudioUtilities.GetMicrophone()
    if devices is None:
        raise RuntimeError("No se encontró micrófono por defecto")
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


def get_taskbar_rect():
    """Obtiene posición y tamaño de la barra de tareas."""
    class APPBARDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uCallbackMessage", wintypes.UINT),
            ("uEdge", wintypes.UINT),
            ("rc", wintypes.RECT),
            ("lParam", wintypes.LPARAM),
        ]
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(abd))  # ABM_GETTASKBARPOS
    return abd.rc


class _AudioMonitor:
    """Stream permanente que publica el nivel RMS actual del mic default."""

    SENSITIVITY = 4.0
    WATCHDOG_INTERVAL_S = 2.0
    BACKOFF_MAX_S = 10.0
    # Si no llega ni un callback de audio en este lapso, el stream se trata
    # como muerto aunque PortAudio siga reportando .active=True. Esto cubre
    # el caso real observado: el stream queda "activo" a nivel PortAudio pero
    # deja de recibir datos (glitch silencioso del host de audio/USB), y sin
    # este chequeo el watchdog nunca lo detecta.
    STALE_CALLBACK_S = 5.0

    def __init__(self):
        self.level = 0.0
        self._stream = None
        self._current_device = None
        self._shutdown = False
        self._backoff = 1.0
        self._last_callback_ts = None
        self._paused = False

        self._open()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        atexit.register(self.close)

    def _callback(self, indata, frames, time_info, status):
        self._last_callback_ts = time.time()
        try:
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            self.level = min(rms * self.SENSITIVITY, 1.0)
        except Exception:
            self.level = 0.0

    def _open(self):
        try:
            device = sd.default.device[0]
            if device is None or device < 0:
                device = sd.query_hostapis()[sd.default.hostapi]["default_input_device"]
            stream = sd.InputStream(
                device=device,
                samplerate=None,
                channels=1,
                dtype="float32",
                blocksize=0,
                latency="low",
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            self._current_device = device
            self._last_callback_ts = time.time()
        except Exception as e:
            print(f"[AudioMonitor] no pude abrir stream: {e}")
            self._stream = None

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def pause(self):
        """Cierra el stream mientras el mic esta muteado: no tiene sentido
        tenerlo abierto 24/7 si el nivel ni se muestra en ese estado, y
        reduce la ventana de exposicion al bug del stream colgado."""
        self._paused = True
        self._close_stream()
        self.level = 0.0

    def resume(self):
        """Reabre el stream al desmutear (stream fresco, sin arrastrar
        ningun estado colgado de antes)."""
        self._paused = False
        if self._stream is None:
            self._open()

    def _watchdog_loop(self):
        while not self._shutdown:
            time.sleep(self.WATCHDOG_INTERVAL_S)
            if self._shutdown:
                return
            if self._paused:
                continue
            try:
                needs_reopen = False
                if self._stream is None:
                    needs_reopen = True
                else:
                    try:
                        if not self._stream.active:
                            needs_reopen = True
                    except Exception:
                        needs_reopen = True
                    if (
                        not needs_reopen
                        and self._last_callback_ts is not None
                        and time.time() - self._last_callback_ts > self.STALE_CALLBACK_S
                    ):
                        print("[AudioMonitor] stream activo pero sin callbacks recientes, reabriendo")
                        needs_reopen = True
                    try:
                        new_default = sd.default.device[0]
                        if new_default is not None and new_default != self._current_device:
                            needs_reopen = True
                    except Exception:
                        pass

                if needs_reopen:
                    self.level = 0.0
                    self._close_stream()
                    self._open()
                    if self._stream is not None:
                        self._backoff = 1.0
                    else:
                        time.sleep(self._backoff)
                        self._backoff = min(self._backoff * 2.0, self.BACKOFF_MAX_S)
            except Exception as e:
                print(f"[AudioMonitor] watchdog error: {e}")

    def close(self):
        self._shutdown = True
        self._close_stream()


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


class MicToggle:
    # Color magico: se vuelve transparente por -transparentcolor.
    # Uso un negro casi puro que no aparece en ningun dibujo.
    TRANSPARENT_BG = "#010101"

    # Paleta
    DISC_COLOR = "#2a2a2a"        # gris oscuro del disco
    MIC_COLOR = "#b5b5b5"         # gris claro del icono mic (activo, en silencio)
    EQ_GREEN = "#4ade80"          # verde de las barras (mismo que la pastilla de dictado)
    MUTED_RED = "#d86666"         # rojo del icono mic cuando esta muteado

    # Proporciones del render (en unidades del supersample). Se calculan
    # relativas al alto del widget (que coincide con el taskbar) al inicializar.
    SUPERSAMPLE = 2

    # Cadencia del tick
    TICK_MS = 40
    MUTE_CHECK_EVERY = 12  # 12 * 40ms = 480ms

    # El mic se transforma en un mini ecualizador de 3 barras cuando detecta
    # voz, con crossfade de opacidad (no un corte duro). Histeresis en los
    # umbrales para que no titile justo en el borde.
    EQ_BAR_COUNT = 3
    EQ_ACTIVATE_LEVEL = 0.08
    EQ_DEACTIVATE_LEVEL = 0.03
    EQ_CROSSFADE_RATE = 0.25
    EQ_BAR_SMOOTH_UP = 0.6
    EQ_BAR_SMOOTH_DOWN = 0.2
    # Geometria de las barras en unidades base-48 (mismo sistema que el
    # icono del mic: se escalan con self._mic_scale).
    EQ_BAR_W = 3
    EQ_BAR_GAP = 2
    EQ_BAR_MIN_H = 3
    EQ_BAR_MAX_H = 12

    def __init__(self):
        self.volume = get_mic_volume()
        self.muted = bool(self.volume.GetMute())
        self.monitor = _AudioMonitor()
        if self.muted:
            self.monitor.pause()
        self._eq_active = False
        self._crossfade = 0.0  # 0 = mic, 1 = barras
        self._bar_levels = [0.0] * self.EQ_BAR_COUNT

        self.root = tk.Tk()
        self.root.title("Mic")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        self.root.configure(bg=self.TRANSPARENT_BG)
        # Hace que el TRANSPARENT_BG se vea transparente (efecto de "widget circular")
        self.root.wm_attributes("-transparentcolor", self.TRANSPARENT_BG)

        # Widget cuadrado del alto del taskbar
        taskbar = get_taskbar_rect()
        taskbar_h = taskbar.bottom - taskbar.top
        w = h = taskbar_h
        x = taskbar.right - w - 200
        y = taskbar.top

        self._widget_size = w  # ancho == alto
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Proporciones relativas al alto del widget
        self._disc_radius = int(w * 0.26)          # ~12 px si w=48
        self._mic_scale = w / 48.0                  # para escalar el mic y las barras

        self.canvas = tk.Canvas(
            self.root, width=w, height=h,
            highlightthickness=0, cursor="hand2", bg=self.TRANSPARENT_BG,
            borderwidth=0,
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_click)

        # Drag para reposicionar
        self._drag_data = {}
        self.canvas.bind("<Button-3>", self._start_drag)
        self.canvas.bind("<B3-Motion>", self._do_drag)

        self._photo = None
        self._canvas_image = self.canvas.create_image(0, 0, anchor="nw", image=None)

        self._tick_count = 0
        self._last_rendered_key = None  # cache para evitar re-render inutil
        self._render(self._current_display_level())
        self._tick()

        # Hotkey global
        self._hotkey_thread = threading.Thread(target=self._listen_hotkey, daemon=True)
        self._hotkey_thread.start()

    def _listen_hotkey(self):
        ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, MOD_CTRL | MOD_SHIFT, VK_F12)
        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message == 0x0312:  # WM_HOTKEY
                self.root.after(0, self._on_click, None)

    def _current_display_level(self):
        """Nivel a mostrar: 0 si muted, sino level comprimido con sqrt."""
        if self.muted:
            return 0.0
        raw = self.monitor.level
        return min(1.0, raw) ** 0.5

    def _render(self, level):
        """Renderiza disco + mic/ecualizador a imagen con Pillow y la muestra."""
        # Cache: muteado es estatico (icono fijo, no hay stream abierto), asi
        # que ahi si vale cachear por key. Activo siempre repinta: el
        # crossfade y las barras animan todo el tiempo.
        if self.muted:
            key = (self.muted, round(level, 2))
            if key == self._last_rendered_key:
                return
            self._last_rendered_key = key
        else:
            self._last_rendered_key = None

        SS = self.SUPERSAMPLE
        W = self._widget_size * SS
        H = self._widget_size * SS

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # transparente
        cx = W // 2
        cy = H // 2

        # ── Disco ──────────────────────────────────────────────
        draw = ImageDraw.Draw(img)
        r_disc = self._disc_radius * SS
        draw.ellipse(
            (cx - r_disc, cy - r_disc, cx + r_disc, cy + r_disc),
            fill=_hex_to_rgb(self.DISC_COLOR),
        )

        # ── Mic / mini ecualizador con crossfade de opacidad ────
        # Modo "RGBA" explicito: con el modo por defecto, ImageDraw
        # reemplaza el pixel en vez de componer el alpha, y el fundido no
        # se veria (todo quedaria 100% opaco u oculto, sin transicion).
        draw = ImageDraw.Draw(img, "RGBA")
        s = self._mic_scale * SS  # mismo factor de escala para mic y barras

        if self.muted:
            self._draw_mic_icon(draw, cx, cy, s, _hex_to_rgb(self.MUTED_RED), 255)
        else:
            mic_alpha = round(255 * (1.0 - self._crossfade))
            if mic_alpha > 0:
                self._draw_mic_icon(draw, cx, cy, s, _hex_to_rgb(self.MIC_COLOR), mic_alpha)
            bar_alpha = round(255 * self._crossfade)
            if bar_alpha > 0:
                self._draw_eq_bars(draw, cx, cy, s, bar_alpha)

        # ── Downscale con antialiasing ──────────────────────────
        img = img.resize((self._widget_size, self._widget_size), Image.LANCZOS)

        # Actualizar imagen en canvas
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self._canvas_image, image=self._photo)

    def _draw_mic_icon(self, draw, cx, cy, s, color, alpha):
        """Dibuja el icono de microfono (capsula + soporte en U + pie)."""
        fill = (*color, alpha)

        cap_w = int(6 * s)
        cap_h = int(11 * s)
        cap_top = cy - int(7 * s)
        cap_bot = cap_top + cap_h
        cap_left = cx - cap_w // 2
        cap_right = cx + cap_w // 2

        try:
            draw.rounded_rectangle(
                (cap_left, cap_top, cap_right, cap_bot),
                radius=cap_w // 2,
                fill=fill,
            )
        except AttributeError:
            # Fallback a rectangle si Pillow viejo
            draw.rectangle((cap_left, cap_top, cap_right, cap_bot), fill=fill)

        u_w = int(12 * s)
        u_h = int(8 * s)
        u_top = cap_bot - int(2 * s)
        u_bbox = (cx - u_w // 2, u_top, cx + u_w // 2, u_top + u_h)
        u_line = max(1, int(1.8 * s))
        # En PIL: start=0, end=180 traza desde 3 CW hasta 9 pasando por 6 (abajo) = U abierta hacia arriba
        draw.arc(u_bbox, start=0, end=180, fill=fill, width=u_line)

        stand_top = u_top + u_h // 2 + int(1 * s)
        stand_bot = stand_top + int(3 * s)
        stand_line = max(1, int(1.8 * s))
        draw.line([(cx, stand_top), (cx, stand_bot)], fill=fill, width=stand_line)

        base_half = int(4 * s)
        draw.line(
            [(cx - base_half, stand_bot), (cx + base_half, stand_bot)],
            fill=fill, width=stand_line,
        )

    def _draw_eq_bars(self, draw, cx, cy, s, alpha):
        """Dibuja las barras del mini ecualizador, centradas en el disco."""
        accent = _hex_to_rgb(self.EQ_GREEN)
        fill = (*accent, alpha)
        bar_w = self.EQ_BAR_W * s
        gap = self.EQ_BAR_GAP * s
        total_w = self.EQ_BAR_COUNT * bar_w + (self.EQ_BAR_COUNT - 1) * gap
        x0 = cx - total_w / 2
        min_h = self.EQ_BAR_MIN_H * s
        max_h = self.EQ_BAR_MAX_H * s
        radius = max(1, bar_w / 2)

        for i, lvl in enumerate(self._bar_levels):
            lvl = max(0.0, min(1.0, lvl))
            x_left = x0 + i * (bar_w + gap)
            x_right = x_left + bar_w
            h = min_h + (max_h - min_h) * lvl
            draw.rounded_rectangle(
                (x_left, cy - h / 2, x_right, cy + h / 2),
                radius=radius,
                fill=fill,
            )

    def _tick(self):
        display = self._current_display_level()

        # Histeresis: activa el ecualizador por encima de un umbral, lo
        # desactiva por debajo de uno mas bajo, asi no titila en el borde.
        if self.muted:
            self._eq_active = False
        elif not self._eq_active and display > self.EQ_ACTIVATE_LEVEL:
            self._eq_active = True
        elif self._eq_active and display < self.EQ_DEACTIVATE_LEVEL:
            self._eq_active = False

        # Crossfade suavizado hacia 0 (mic) o 1 (barras), no un corte duro.
        target_fade = 1.0 if self._eq_active else 0.0
        self._crossfade += (target_fade - self._crossfade) * self.EQ_CROSSFADE_RATE

        # Nivel por barra, con modulacion propia para que no se muevan todas
        # identicas (mismo patron que las barras de la pastilla de dictado).
        t = time.time()
        for i in range(self.EQ_BAR_COUNT):
            freq = 3 + i
            mod = 0.5 + 0.5 * math.sin(t * freq * 2 * math.pi + i)
            target = display * mod if self._eq_active else 0.0
            cur = self._bar_levels[i]
            rate = self.EQ_BAR_SMOOTH_UP if target > cur else self.EQ_BAR_SMOOTH_DOWN
            self._bar_levels[i] = cur + (target - cur) * rate

        self._render(display)

        self._tick_count += 1
        if self._tick_count >= self.MUTE_CHECK_EVERY:
            self._tick_count = 0
            try:
                real_muted = bool(self.volume.GetMute())
            except Exception:
                try:
                    self.volume = get_mic_volume()
                    real_muted = bool(self.volume.GetMute())
                except Exception:
                    real_muted = self.muted
            if real_muted != self.muted:
                self.muted = real_muted
                (self.monitor.pause() if real_muted else self.monitor.resume())
                self._last_rendered_key = None  # forzar re-render
                self._render(self._current_display_level())

        try:
            self.root.after(self.TICK_MS, self._tick)
        except tk.TclError:
            return

    def _on_click(self, event):
        try:
            current = self.volume.GetMute()
            self.volume.SetMute(not current, None)
            self.muted = not current
        except Exception:
            self.volume = get_mic_volume()
            current = self.volume.GetMute()
            self.volume.SetMute(not current, None)
            self.muted = not current
        (self.monitor.pause() if self.muted else self.monitor.resume())
        self._last_rendered_key = None
        self._render(self._current_display_level())

    def _start_drag(self, event):
        self._drag_data = {"x": event.x}

    def _do_drag(self, event):
        dx = event.x - self._drag_data["x"]
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y()
        self.root.geometry(f"+{x}+{y}")

    def _keep_visible(self):
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(5000, self._keep_visible)

    def run(self):
        self._keep_visible()
        self.root.mainloop()


if __name__ == "__main__":
    try:
        app = MicToggle()
        print(f"Mic: {'MUTEADO' if app.muted else 'ACTIVO'}")
        print("Click izquierdo = toggle | Click derecho + arrastrar = mover")
        app.run()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("Presiona Enter para cerrar...")
