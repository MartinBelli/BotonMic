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

a = Analysis(
    ['mic_dictado.py'],
    pathex=[],
    binaries=fw_binaries + ct_binaries + tk_binaries + sd_binaries + ort_binaries,
    datas=fw_datas + ct_datas + tk_datas + sd_datas + ort_datas,
    hiddenimports=(
        fw_hidden + ct_hidden + tk_hidden + ort_hidden
        + ['ctranslate2', 'tokenizers']
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
