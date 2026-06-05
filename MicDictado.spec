# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import (
    collect_all, collect_dynamic_libs, collect_data_files,
)

# faster-whisper depende de ctranslate2 (DLLs nativas) y tokenizers.
# collect_all junta data + binaries + hidden imports en una sola llamada.
fw_datas, fw_binaries, fw_hidden = collect_all('faster_whisper')
ct_datas, ct_binaries, ct_hidden = collect_all('ctranslate2')
tk_datas, tk_binaries, tk_hidden = collect_all('tokenizers')

# sounddevice embebe la DLL de PortAudio
sd_binaries = collect_dynamic_libs('sounddevice')
sd_datas = collect_data_files('sounddevice')

# onnxruntime puede llegar como dep transitiva (VAD opcional de faster-whisper).
# Lo agregamos por las dudas; si no esta instalado, collect_all retorna vacio.
try:
    ort_datas, ort_binaries, ort_hidden = collect_all('onnxruntime')
except Exception:
    ort_datas, ort_binaries, ort_hidden = [], [], []

# llama-cpp-python: backend del modo limpio. Trae libllama / libggml (DLLs)
# y se importa lazy dentro de mic_dictado.py, asi que necesitamos collect_all
# para que PyInstaller no se lo pierda. El modelo GGUF NO va en el exe;
# vive en %LOCALAPPDATA%\MicDictado\llm\.
try:
    llm_datas, llm_binaries, llm_hidden = collect_all('llama_cpp')
except Exception:
    llm_datas, llm_binaries, llm_hidden = [], [], []

# DLLs CUDA: nvidia-cublas-cu12 / nvidia-cudnn-cu12 / nvidia-cuda-nvrtc-cu12.
# En dev las carga cuda_dlls.py via os.add_dll_directory() apuntando a
# site-packages\nvidia\<pkg>\bin\. En el .exe ese path no existe, asi que
# hay que empaquetar las DLLs y replicarlas adentro del bundle, manteniendo
# la estructura nvidia/<pkg>/bin/ para que cuda_dlls.py las encuentre por
# importlib.util.find_spec.
cuda_datas, cuda_binaries, cuda_hidden = [], [], []
for _pkg in ('cublas', 'cudnn', 'cuda_nvrtc'):
    try:
        _d, _b, _h = collect_all(f'nvidia.{_pkg}')
        cuda_datas += _d
        cuda_binaries += _b
        cuda_hidden += _h
    except Exception:
        pass

a = Analysis(
    ['mic_dictado.py'],
    pathex=[],
    binaries=fw_binaries + ct_binaries + tk_binaries + sd_binaries + ort_binaries + llm_binaries + cuda_binaries,
    datas=fw_datas + ct_datas + tk_datas + sd_datas + ort_datas + llm_datas + cuda_datas,
    hiddenimports=(
        fw_hidden + ct_hidden + tk_hidden + ort_hidden + llm_hidden + cuda_hidden
        + ['ctranslate2', 'tokenizers', 'llama_cpp']
        # Backends de pystray para Windows
        + ['pystray._win32', 'pystray._base']
        # Modulos propios cargados via 'from ... import ...' a nivel de funcion
        + ['settings', 'audio_ducker', 'tray']
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MicDictado',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
