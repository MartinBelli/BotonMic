"""
audio_ducker.py — Baja el volumen de las apps con audio mientras grabas.

Usa pycaw.AudioUtilities.GetAllSessions() para listar sesiones de audio activas
y bajar el volumen de cada una al porcentaje configurado, salvo procesos en la
lista de exclusion (Discord, Teams, etc.) y el propio MicDictado.

duck() guarda el volumen previo de cada sesion. restore() lo devuelve a su
valor original. Si una sesion desaparece entre duck/restore, se ignora.
"""

import os
import threading

from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

from settings import settings


_SELF_PROCESS = os.path.basename(os.environ.get("MIC_DICTADO_EXE", "MicDictado.exe")).lower()


class AudioDucker:
    def __init__(self):
        self._saved = []  # list of (volume_iface, prev_volume)
        self._lock = threading.Lock()
        self._is_ducked = False

    def duck(self):
        """Baja el volumen de las apps con audio segun settings. Idempotente."""
        if not settings.ducking_enabled:
            return
        with self._lock:
            if self._is_ducked:
                return
            target_pct = max(0, min(100, int(settings.ducking_volume_pct))) / 100.0
            exclude = {p.lower() for p in settings.ducking_exclude}

            try:
                sessions = AudioUtilities.GetAllSessions()
            except Exception as e:
                print(f"[ducker] no pude enumerar sesiones: {e}")
                return

            saved = []
            for s in sessions:
                try:
                    proc = s.Process
                    if proc is None:
                        # Sesion del sistema (sounds, etc.) - la dejamos
                        continue
                    pname = proc.name().lower()
                    if pname == _SELF_PROCESS or pname in exclude:
                        continue
                    vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                    prev = float(vol.GetMasterVolume())
                    if prev <= 0.0:
                        # Ya esta en 0, no hace falta tocar
                        continue
                    vol.SetMasterVolume(prev * target_pct, None)
                    saved.append((vol, prev))
                except Exception as e:
                    # Sesiones individuales pueden fallar (procesos zombies, COM); seguimos
                    print(f"[ducker] sesion ignorada: {e}")
                    continue

            self._saved = saved
            self._is_ducked = True

    def restore(self):
        """Restaura los volumenes guardados por duck(). Idempotente."""
        with self._lock:
            if not self._is_ducked:
                return
            for vol, prev in self._saved:
                try:
                    vol.SetMasterVolume(prev, None)
                except Exception:
                    # La sesion pudo haber muerto; la ignoramos
                    pass
            self._saved = []
            self._is_ducked = False
