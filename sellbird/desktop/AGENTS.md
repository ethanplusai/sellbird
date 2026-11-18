# DESKTOP CLIENT

## OVERVIEW
Optional macOS menu-bar and Windows tray/autostart client; score 8, distinct platform boundary.

## WHERE TO LOOK
| Task | Location | Contract |
|------|----------|----------|
| Dashboard client | `tray.py` | `TrayController`, `TrayApp`, `run_tray` |
| Status/menu state | `tray.py` | `TrayStatus`, `MenuRow`, `menu_items` |
| Icon drawing | `tray.py` | `bird_points`, `draw_icon`, `make_icon`, `menubar_png` |
| Login installation | `login_item.py` | `install`, `uninstall`, `status` |
| macOS ownership | `login_item.py` | Label plus embedded plist marker |
| Windows ownership | `login_item.py` | Startup shortcut Description marker |
| Command registration | `../desktop_cli.py` | Tray/autostart argparse handlers |
| Dashboard identity | `../dashboard_instance.py` | Published instance and matching API identity |

## CONVENTIONS
- Tray is a dashboard client, never a second HTTP server or independent heartbeat.
- Before credentials are sent, `/api/instance` must match the published dashboard instance ID.
- Starting the dashboard launches a detached process; closing tray leaves that process running.
- Browser opening passes the fragment unlock link directly to the OS opener.
- Optional pystray/Pillow imports stay inside functions; Linux operates without this tray.
- Menu operations must not block the UI thread; schedule UI work through the platform scheduler.
- macOS login uses a per-user LaunchAgent; Windows uses a per-user Startup shortcut.
- Login launch command is `python -P -m sellbird tray`, not a copied script.
- Login-item OS interactions accept injected runner, environment and home for testing.
- Recognize ownership before changing an existing startup entry.
- Launchctl bootout targets only Sellbird's label.
- Focused checks: `python -m pytest -q tests/test_tray.py tests/test_login_item.py`.

## ANTI-PATTERNS
- Do not print or log dashboard unlock URLs, including status/error paths.
- Do not hand credentials to an unrelated server that happens to occupy the remembered port.
- Do not overwrite foreign/unrecognized plist or shortcut entries.
- Do not replace symlinked startup entries.
- Do not import tray UI dependencies into login-item handling.
- Do not let tests invoke real launchctl or modify real LaunchAgents/Startup folders.
- Do not replace per-user shortcuts with elevated/system-wide startup tasks.
