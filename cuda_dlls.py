"""
cuda_dlls.py — Setup de DLLs CUDA para Windows.

Cuando ctranslate2 corre en CUDA, necesita encontrar cublas64_12.dll,
cudnn*_9.dll y nvrtc*.dll. Si CUDA Toolkit no esta instalado en el sistema,
esas DLLs vienen con los packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 /
nvidia-cuda-nvrtc-cu12 en site-packages, pero Windows NO las descubre solas:
hay que registrar manualmente los directorios bin/ con os.add_dll_directory()
antes de importar faster_whisper.

Llamar a setup_cuda_dlls() ANTES del primer `from faster_whisper import ...`.

Es no-op fuera de Windows y cuando los packages nvidia-* no estan presentes
(caso CPU-only), por lo que se puede invocar siempre sin if-around-everything.
"""

import ctypes
import glob
import importlib.util
import os
import sys


_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")

# Orden de precarga: las DLLs base primero, luego las que dependen de ellas.
# nvrtc no depende de nadie. cublas depende de nvrtc. cudnn depende de cublas.
# Se intenta cargar todas las del directorio igual; este orden minimiza errores
# de "dependent library not found" en la primera pasada.
_PRELOAD_ORDER = ("cuda_nvrtc", "cublas", "cudnn")


def setup_cuda_dlls():
    """Registra dirs y pre-carga DLLs de los packages nvidia-* en el proceso.

    Hace dos cosas:

    1. os.add_dll_directory() para cada bin/. Suficiente para la mayoria de
       libs Python que respetan el DLL search path de Windows moderno.

    2. ctypes.WinDLL() pre-carga las .dll principales. ctranslate2 hace
       LoadLibrary por nombre con flags que NO respetan add_dll_directory en
       todas las versiones, asi que precargar las DLLs por path absoluto las
       deja en memoria del proceso y cualquier LoadLibrary posterior las
       encuentra por handle, no por search path.

    Devuelve (dirs_registrados, dlls_precargadas) para diagnosticos."""
    if sys.platform != "win32":
        return [], []

    pkg_to_bin = {}
    for pkg in _PACKAGES:
        spec = importlib.util.find_spec(f"nvidia.{pkg}")
        if spec is None or not spec.submodule_search_locations:
            continue
        for base in spec.submodule_search_locations:
            bin_dir = os.path.join(base, "bin")
            if os.path.isdir(bin_dir):
                pkg_to_bin[pkg] = bin_dir

    registered = []
    for bin_dir in pkg_to_bin.values():
        try:
            os.add_dll_directory(bin_dir)
            registered.append(bin_dir)
        except (OSError, AttributeError):
            pass

    preloaded = []
    for pkg in _PRELOAD_ORDER:
        bin_dir = pkg_to_bin.get(pkg)
        if not bin_dir:
            continue
        for dll_path in sorted(glob.glob(os.path.join(bin_dir, "*.dll"))):
            try:
                ctypes.WinDLL(dll_path)
                preloaded.append(dll_path)
            except OSError:
                # Algunas DLLs (alt, redistributables) pueden fallar; ignorar.
                # Lo critico es cublas64_12.dll y cudnn_*64_9.dll; si esas
                # cargan, el resto del runtime CUDA arranca.
                pass
    return registered, preloaded
