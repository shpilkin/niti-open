# PyInstaller spec for the Email Reader backend (onedir).
#
# Build from the repo root:
#   pyinstaller packaging/email_reader_backend.spec --noconfirm
#
# Produces dist/email-reader-backend/ — a self-contained backend the macOS
# shell spawns. Read-only assets (templates, static, alembic) are bundled as
# datas and resolved at runtime via email_reader.paths.resource_path().

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = os.path.abspath(os.getcwd())

datas = [
    (os.path.join(project_root, "templates"), "templates"),
    (os.path.join(project_root, "static"), "static"),
    (os.path.join(project_root, "alembic"), "alembic"),
]
# config.example.yaml is handy for first-run scaffolding.
example_cfg = os.path.join(project_root, "config", "config.example.yaml")
if os.path.exists(example_cfg):
    datas.append((example_cfg, "config"))

# alembic loads versions/env.py dynamically, so PyInstaller can't trace them.
hiddenimports = []
hiddenimports += collect_submodules("aiosqlite")
hiddenimports += collect_submodules("sqlalchemy.dialects.sqlite")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("email_reader")
# caldav lazy-loads its whole public API via PEP 562 (__getattr__ -> importlib), which
# PyInstaller's modulegraph cannot trace — without this the frozen app bundles only
# caldav/__init__.py and calendar sync dies with ModuleNotFoundError at runtime.
hiddenimports += collect_submodules("caldav")
hiddenimports += ["icalendar"]
# snowballstemmer resolves stemmer("russian") to a module by name at runtime, which
# modulegraph cannot follow — without this the search index has no Russian stems in
# the .app and finds only the exact word forms.
hiddenimports += collect_submodules("snowballstemmer")
# The MCP server is imported only when the endpoint mounts, so modulegraph never reaches
# it from the entry point. Subpackage by subpackage, not the whole of `mcp`: its `cli`
# module needs typer, which this app does not install, and collecting it aborts the build.
hiddenimports += collect_submodules("mcp.server")
hiddenimports += collect_submodules("mcp.shared")
hiddenimports += collect_submodules("mcp.types")
hiddenimports += [
    "anthropic",
    "openai",
    "aiogram",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers.interval",
]

# Some libs ship data files (tokenizers, certs, templates).
datas += collect_data_files("anthropic")
datas += collect_data_files("certifi")

a = Analysis(
    [os.path.join(project_root, "packaging", "backend_entry.py")],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="email-reader-backend",
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="email-reader-backend",
)
