"""
mic_tray.py — Toggle de micrófono en la barra de tareas de Windows.

Widget "circular" (transparentcolor) con disco gris, micrófono gris claro
en el centro, y un arco de halo de las 7 a las 5 (pasando por arriba).
Cuando el mic está activo el arco se pinta de verde claro; al hablar se
sobrepinta con verde saturado creciendo de izquierda (7) a derecha (5),
funcionando como VU meter angular. Muteado: arco en rojo tenue.

Render con Pillow + supersampling 2x para antialiasing.

Requiere: pip install -r requirements.txt
"""

import atexit
import ctypes
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

    def __init__(self):
        self.level = 0.0
        self._stream = None
        self._current_device = None
        self._shutdown = False
        self._backoff = 1.0

        self._open()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        atexit.register(self.close)

    def _callback(self, indata, frames, time_info, status):
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

    def _watchdog_loop(self):
        while not self._shutdown:
            time.sleep(self.WATCHDOG_INTERVAL_S)
            if self._shutdown:
                return
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
    MIC_COLOR = "#b5b5b5"         # gris claro del icono mic
    HALO_BASE_GREEN = "#9fd9b1"   # verde pastel claro (reposo)
    HALO_FILL_GREEN = "#3fa861"   # verde saturado (hablando)
    HALO_BASE_RED = "#d86666"     # rojo tenue (muteado)

    # Proporciones del render (en unidades del supersample). Se calculan
    # relativas al alto del widget (que coincide con el taskbar) al inicializar.
    SUPERSAMPLE = 2

    # Cadencia del tick
    TICK_MS = 40
    MUTE_CHECK_EVERY = 12  # 12 * 40ms = 480ms

    # Arco del halo: convención PIL (0° = 3 en reloj, CW+).
    # 7 en reloj = 120° PIL, 5 = 60° PIL. De 7 a 5 pasando por arriba = 300°.
    HALO_START = 120
    HALO_EXTENT = 300

    def __init__(self):
        self.volume = get_mic_volume()
        self.muted = bool(self.volume.GetMute())
        self.monitor = _AudioMonitor()

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
        self._halo_inner_r = self._disc_radius + max(2, int(w * 0.06))
        self._halo_width = max(4, int(w * 0.11))   # grosor del arco
        self._halo_outer_r = self._halo_inner_r + self._halo_width
        self._mic_scale = w / 48.0                  # para escalar el mic

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
        """Renderiza el halo+disco+mic a imagen con Pillow y la muestra."""
        # Cache: solo re-renderizar si algo cambió significativamente
        # (level redondeado a 0.02 + estado muted). Evita ~30 renders/seg inutiles.
        key = (self.muted, round(level, 2))
        if key == self._last_rendered_key:
            return
        self._last_rendered_key = key

        SS = self.SUPERSAMPLE
        W = self._widget_size * SS
        H = self._widget_size * SS

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # transparente
        draw = ImageDraw.Draw(img)

        cx = W // 2
        cy = H // 2

        # ── Halo ──────────────────────────────────────────────
        halo_outer_ss = self._halo_outer_r * SS
        halo_inner_ss = self._halo_inner_r * SS
        halo_center_r = (halo_outer_ss + halo_inner_ss) // 2
        halo_w_ss = halo_outer_ss - halo_inner_ss

        halo_bbox = (cx - halo_center_r, cy - halo_center_r,
                     cx + halo_center_r, cy + halo_center_r)

        if self.muted:
            base_color = _hex_to_rgb(self.HALO_BASE_RED)
            # En muted, el halo es tenue constante (sin fill).
            draw.arc(
                halo_bbox,
                start=self.HALO_START,
                end=self.HALO_START + self.HALO_EXTENT,
                fill=base_color,
                width=halo_w_ss,
            )
        else:
            base_color = _hex_to_rgb(self.HALO_BASE_GREEN)
            fill_color = _hex_to_rgb(self.HALO_FILL_GREEN)

            # Capa base: arco completo verde claro
            draw.arc(
                halo_bbox,
                start=self.HALO_START,
                end=self.HALO_START + self.HALO_EXTENT,
                fill=base_color,
                width=halo_w_ss,
            )

            # Capa fill: arco verde saturado que crece de 7 a 5 con el nivel
            if level > 0.01:
                fill_end = self.HALO_START + self.HALO_EXTENT * min(1.0, level)
                draw.arc(
                    halo_bbox,
                    start=self.HALO_START,
                    end=fill_end,
                    fill=fill_color,
                    width=halo_w_ss,
                )

        # ── Disco ──────────────────────────────────────────────
        r_disc = self._disc_radius * SS
        draw.ellipse(
            (cx - r_disc, cy - r_disc, cx + r_disc, cy + r_disc),
            fill=_hex_to_rgb(self.DISC_COLOR),
        )

        # ── Icono del mic (mas chico, centrado en el disco) ────
        mc = _hex_to_rgb(self.MIC_COLOR)
        s = self._mic_scale * SS  # factor escala

        cap_w = int(6 * s)
        cap_h = int(11 * s)
        cap_top = cy - int(7 * s)
        cap_bot = cap_top + cap_h
        cap_left = cx - cap_w // 2
        cap_right = cx + cap_w // 2

        # Cápsula redondeada (cuerpo del mic)
        try:
            draw.rounded_rectangle(
                (cap_left, cap_top, cap_right, cap_bot),
                radius=cap_w // 2,
                fill=mc,
            )
        except AttributeError:
            # Fallback a rectangle si Pillow viejo
            draw.rectangle((cap_left, cap_top, cap_right, cap_bot), fill=mc)

        # Soporte en U debajo del cuerpo
        u_w = int(12 * s)
        u_h = int(8 * s)
        u_top = cap_bot - int(2 * s)
        u_bbox = (cx - u_w // 2, u_top, cx + u_w // 2, u_top + u_h)
        u_line = max(1, int(1.8 * s))
        # En PIL: start=0, end=180 traza desde 3 CW hasta 9 pasando por 6 (abajo) = U abierta hacia arriba
        draw.arc(u_bbox, start=0, end=180, fill=mc, width=u_line)

        # Pie del mic
        stand_top = u_top + u_h // 2 + int(1 * s)
        stand_bot = stand_top + int(3 * s)
        stand_line = max(1, int(1.8 * s))
        draw.line([(cx, stand_top), (cx, stand_bot)], fill=mc, width=stand_line)

        # Base del pie
        base_half = int(4 * s)
        draw.line(
            [(cx - base_half, stand_bot), (cx + base_half, stand_bot)],
            fill=mc, width=stand_line,
        )

        # ── Downscale con antialiasing ──────────────────────────
        img = img.resize((self._widget_size, self._widget_size), Image.LANCZOS)

        # Actualizar imagen en canvas
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self._canvas_image, image=self._photo)

    def _tick(self):
        display = self._current_display_level()
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
