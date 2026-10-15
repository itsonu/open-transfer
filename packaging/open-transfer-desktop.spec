# -*- mode: python ; coding: utf-8 -*-
# The double-clickable desktop app: Open Transfer in its own native window.
#
#   python scripts/build_app.py --desktop      (installs pywebview, builds, packages, smoke-tests)
#
# or by hand, with `pip install ".[desktop]" pyinstaller pillow` done first:
#
#   pyinstaller --noconfirm packaging/open-transfer-desktop.spec
#
# Windows: dist/Open Transfer.exe   one windowed file; the UI runs in WebView2 (pythonnet).
# macOS:   dist/Open Transfer.app   a bundle (onedir: PyInstaller is retiring onefile .app
#                                   bundles, and they start slower); the UI runs in WKWebView
#                                   (PyObjC). build_app.py wraps it in a .dmg.
#
# The command-line build (packaging/open-transfer.spec) is separate and unchanged.
import os
import re
import sys
from importlib.util import find_spec

from PyInstaller.utils.hooks import collect_data_files

HERE = SPECPATH  # noqa: F821 - defined by PyInstaller
ROOT = os.path.dirname(HERE)
NAME = "Open Transfer"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

with open(os.path.join(ROOT, "src", "open_transfer", "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'__version__ = "(.+?)"', f.read())[1]


def available(*modules):
    """Hidden imports that are actually installed (missing ones only cause warnings anyway)."""
    found = []
    for module in modules:
        try:
            if find_spec(module) is not None:
                found.append(module)
        except (ImportError, ValueError):
            pass
    return found


# open_transfer.desktop imports pywebview with importlib (it's optional), which
# PyInstaller can't see, so name it and the backend for this OS. pywebview ships
# its own PyInstaller hook (webview/__pyinstaller) that collects its JavaScript
# and, on Windows, the WebView2 DLLs in webview/lib. pyinstaller-hooks-contrib's
# hook-clr / hook-clr_loader add pythonnet's Python.Runtime.dll and ClrLoader DLLs.
hiddenimports = ["cheroot.ssl.builtin", "open_transfer.desktop", "webview"]
if IS_WIN:
    hiddenimports += available(
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "webview.platforms.mshtml",  # only to detect a missing WebView2 runtime cleanly
        "webview.platforms.win32",
        "clr",
        "clr_loader",
        "pythonnet",
    )
elif IS_MAC:
    # PyObjC's framework wrappers are plain packages + extension modules, which
    # PyInstaller follows from these imports (cocoa.py imports UniformTypeIdentifiers lazily).
    hiddenimports += available(
        "webview.platforms.cocoa",
        "objc",
        "Foundation",
        "AppKit",
        "WebKit",
        "UniformTypeIdentifiers",
        "PyObjCTools.AppHelper",
    )

# Found through the installed package (build_app.py installs it first); without
# them the app would start but every page would be a 500 error.
datas = collect_data_files("open_transfer")
if not any(dest.startswith(os.path.join("open_transfer", "templates")) for _, dest in datas):
    raise SystemExit("open_transfer's templates weren't found: pip install . first")

a = Analysis(
    [os.path.join(HERE, "desktop_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    # pywebview imports every backend it supports; keep the ones we don't use (and
    # whatever GUI toolkits happen to be installed on the build machine) out.
    excludes=[
        "tkinter",
        "pytest",
        "playwright",
        "webview.platforms.qt",
        "webview.platforms.gtk",
        "webview.platforms.cef",
        "webview.platforms.android",
        "qtpy",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gi",
        "cefpython3",
        "kivy",
        "jnius",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

if IS_MAC:
    icns = os.path.join(ROOT, "build", "icons", "open-transfer.icns")  # scripts/make_icons.py
    # Fallback: PyInstaller converts the PNG to .icns itself when Pillow is installed.
    icon = icns if os.path.exists(icns) else os.path.join(
        ROOT, "src", "open_transfer", "static", "icons", "icon-512.png"
    )
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        target_arch=None,  # the build machine's: arm64 on Apple silicon, x86_64 on Intel
        codesign_identity=None,  # ad-hoc; build_app.py re-signs the finished bundle
        entitlements_file=None,
        icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME, strip=False, upx=False)
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="io.github.itsonu.opentransfer",
        version=VERSION,
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.utilities",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # follow Dark Mode
            "NSHumanReadableCopyright": "MIT License, Open Transfer contributors",
            # macOS 15+ asks before an app talks to the local network. Discovery is raw
            # UDP multicast/broadcast (not Bonjour), so no NSBonjourServices entry is
            # needed and no multicast entitlement exists or is needed on macOS (unlike
            # iOS); this text is what the permission prompt shows.
            "NSLocalNetworkUsageDescription": (
                "Open Transfer finds and sends files to your other devices on this Wi-Fi."
            ),
            # The UI can scan a pairing QR code with the camera (getUserMedia in WKWebView).
            # With the hardened runtime (notarized builds) also add the
            # com.apple.security.device.camera entitlement.
            "NSCameraUsageDescription": "Scan another device's QR code to pair.",
            # Received files go to ~/Downloads/Open Transfer by default.
            "NSDownloadsFolderUsageDescription": (
                "Open Transfer saves the files you receive in Downloads."
            ),
            # The window loads the app's own server over plain HTTP on 127.0.0.1.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
else:
    version_info = None
    if IS_WIN:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )

        parts = [int(n) for n in re.findall(r"\d+", VERSION)][:4]
        numbers = tuple(parts + [0] * (4 - len(parts)))
        version_info = VSVersionInfo(
            ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
            kids=[
                StringFileInfo(
                    [
                        StringTable(
                            "040904B0",
                            [
                                StringStruct("CompanyName", "Open Transfer contributors"),
                                StringStruct("FileDescription", NAME),
                                StringStruct("FileVersion", VERSION),
                                StringStruct("InternalName", NAME),
                                StringStruct("LegalCopyright", "MIT License"),
                                StringStruct("OriginalFilename", f"{NAME}.exe"),
                                StringStruct("ProductName", NAME),
                                StringStruct("ProductVersion", VERSION),
                            ],
                        )
                    ]
                ),
                VarFileInfo([VarStruct("Translation", [1033, 1200])]),
            ],
        )
    # One windowed file. Onefile also sidesteps .NET refusing DLLs that carry the
    # downloaded-from-the-internet mark: they're unpacked fresh at every start.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        icon=[os.path.join(HERE, "app_icon.ico")],
        version=version_info,
    )
