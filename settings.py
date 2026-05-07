"""
settings.py — Configuracion persistente de MicDictado.

JSON en %APPDATA%\\MicDictado\\settings.json. Singleton 'settings' cargado al
importar el modulo. Tolera archivos viejos / claves nuevas via merge sobre
DEFAULTS.
"""

import json
import os
import tempfile
import threading


DEFAULT_INITIAL_PROMPT = (
    "Transcripción en español argentino. Vocabulario técnico habitual: "
    "Next.js, React, TypeScript, Tailwind, Vercel, Supabase, Cloudflare, "
    "Cursor, Claude, Anthropic, Santex, Martin Belli, GitHub, pull request, "
    "merge, deploy, endpoint, middleware, hook, prompt, repo, branch, commit, "
    "PyInstaller, Whisper, faster-whisper, MicDictado, "
    "Llama, Llama 3.2, tool calling, GGUF, int8, int4, Q4, Q4_K_M, "
    "OpenAI, GPT, RTX 2060, RTX 4070, CUDA, GPU, CPU, RAM, VRAM, "
    "Ollama, llama.cpp, Hugging Face, embedding, fine-tuning, LLM."
)


DEFAULTS = {
    # VU meter
    "vu_gain": 7.0,                  # 1.0 - 15.0 (mas alto = mas sensible)

    # Ducking de musica
    "ducking_enabled": True,
    "ducking_volume_pct": 25,        # 0 - 100, % del volumen original
    "ducking_exclude": [
        "Discord.exe",
        "Teams.exe",
        "ms-teams.exe",
        "Zoom.exe",
        "slack.exe",
    ],

    # Transcripcion
    "model_name": "small",           # tiny | base | small | medium | large-v3-turbo
    # device/compute_type: 'auto' detecta GPU y elige float16 si hay CUDA, sino
    # cae a CPU+int8. El usuario puede forzar cualquier combinacion. Si CUDA
    # falla al cargar, _load_model hace fallback transparente a CPU+int8.
    "device": "auto",                # auto | cpu | cuda
    "compute_type": "auto",          # auto | int8 | int8_float16 | float16 | float32
    "beam_size": 1,                  # 1 = greedy, mas alto = mas preciso/lento
    "vad_filter": True,
    # initial_prompt: contexto/vocabulario que se le pasa a Whisper como "final
    # de transcripcion previa". Sesga al modelo hacia palabras escritas asi.
    # Ideal para nombres propios, marcas, anglicismos tecnicos.
    # Whisper solo guarda los ultimos ~224 tokens, mantenerlo corto.
    "initial_prompt": DEFAULT_INITIAL_PROMPT,

    # Historial de transcripciones (texto plano, sin audio)
    "history_enabled": True,
    "history_max_entries": 500,
}


_SETTINGS_DIR = os.path.join(
    os.environ.get("APPDATA", tempfile.gettempdir()),
    "MicDictado",
)
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "settings.json")


class _Settings:
    """Singleton de configuracion. Acceso a claves via atributos: settings.vu_gain."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self._load()

    def _load(self):
        if not os.path.exists(_SETTINGS_FILE):
            return
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                merged = dict(DEFAULTS)
                for k, v in disk.items():
                    if k in DEFAULTS:
                        merged[k] = v
                self._data = merged
        except (OSError, json.JSONDecodeError) as e:
            print(f"[settings] error leyendo {_SETTINGS_FILE}: {e}; uso defaults")

    def save(self):
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            tmp = _SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, _SETTINGS_FILE)
        except OSError as e:
            print(f"[settings] error guardando {_SETTINGS_FILE}: {e}")

    def update(self, **kwargs):
        """Actualiza claves validas y persiste a disco."""
        with self._lock:
            for k, v in kwargs.items():
                if k in DEFAULTS:
                    self._data[k] = v
            self.save()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in DEFAULTS:
            return self._data.get(name, DEFAULTS[name])
        raise AttributeError(name)


settings = _Settings()
