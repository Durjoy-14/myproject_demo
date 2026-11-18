# PC Controller

Control your Windows PC from your iPhone over your home Wi-Fi. Run one small
program on the PC, scan a QR code with the iPhone camera, and a control panel
opens in Safari. No app to install on the phone.

Version 3 (keyboard + brightness).

## What it can do

| Tab | Features |
| --- | --- |
| **Power** | Shut down, restart, sleep, hibernate, lock, sign out, turn the screen off, shut-down timer (15 min / 30 min / 1 h / 2 h), cancel a pending shutdown |
| **Media** | Volume up/down, mute, play/pause, next, previous, screen brightness slider |
| **Mouse** | Phone as a large trackpad (tap = click, two-finger tap = right-click, two-finger drag = scroll), click buttons, type text on the PC, quick keys |
| **Keys** | Full on-screen keyboard: letters, numbers, symbols, F1-F12, arrows, Shift/Ctrl/Alt/Win combos, Copy/Paste/Cut/Undo/Select all/Save shortcuts |
| **Screen** | See your PC screen on the phone, auto-refresh, tap the picture to click |
| **Apps** | Launch apps, open any website on the PC |
| **Status** | CPU, memory, battery, disk space, uptime, PC clock |

## Files

| File | Purpose |
| --- | --- |
| `pc_controller.py` | The whole program (server + phone page) |
| `start_controller.bat` | Double-click launcher for Windows |

## Setup

### On the Windows PC

1. **Install Python** from <https://www.python.org/downloads/>. On the first
   install screen, tick **"Add python.exe to PATH"**.
2. **Put both files in one folder**, for example `C:\PCController`. The Python
   file must be named exactly `pc_controller.py` (not `pc_controller (1).py`).
3. **Set your PIN.** Open `pc_controller.py` in Notepad and change
   `PIN = "1234"` to your own PIN. Save.
4. **Start it.** Double-click `start_controller.bat`.
   - The first run installs two small packages (`segno` for the QR code and
     `pillow` for the screen view). The PC needs internet for this once.
   - A browser window opens on the PC showing a big QR code.
5. **Allow the firewall prompt.** Tick **Private networks** and click **Allow**.
   Without this, the phone cannot connect.

### On the iPhone

1. Connect to the **same Wi-Fi** as the PC.
2. Open the **Camera** app, point it at the QR code, and tap the link.
3. The control panel opens, already signed in.
4. Optional: in Safari tap **Share > Add to Home Screen** for a one-tap icon.
   (The icon asks for your PIN the first time.)

Keep the black controller window open. Closing it stops the controller.

### Start automatically with Windows (optional)

1. Press `Win + R`, type `shell:startup`, press Enter.
2. Drag a shortcut of `start_controller.bat` into that folder.

## Settings

All settings are at the top of `pc_controller.py`. Open it in Notepad to edit.

| Setting | Default | Meaning |
| --- | --- | --- |
| `PIN` | `"1234"` | Your PIN. **Change this.** |
| `PORT` | `8765` | Port the controller listens on |
| `DELAY` | `15` | Seconds before shutdown/restart happens, so you can cancel |
| `MAX_FAILS` / `LOCKOUT_SECONDS` | `5` / `60` | Wrong-PIN attempts allowed before a 60-second lockout |
| `QR_INCLUDES_PIN` | `True` | `True`: scanning the QR logs you in. `False`: you type the PIN after scanning |
| `OPEN_BROWSER` | `True` | Open the QR page on the PC automatically at start |
| `FEATURES` | all `True` | Set any feature to `False` to switch it off (its tab disappears) |
| `APPS` | see file | Buttons on the Apps tab: `"Name": "program"` or `"Name": "https://site"` |

Example: to add a button for VLC, add `"VLC": "vlc",` inside `APPS`.

After changing a setting, close the controller window and start it again.

## Security

- The controller is reachable only on your local network. It is not exposed to
  the internet.
- Every action needs your PIN. After 5 wrong attempts it locks out for 60 seconds.
- The QR page opens only on the PC itself, because the QR code contains your PIN.
- The connection is plain HTTP, so anyone on your Wi-Fi who learns the PIN could
  control the PC. Use a PIN that is not easy to guess, and do not run this on
  public or shared Wi-Fi.
- Screen view, typing and the keyboard give a lot of control. Turn off what you
  do not need in `FEATURES`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Phone cannot connect | Same Wi-Fi (not a guest network)? VPN off on the phone? Firewall prompt allowed on Private networks? Controller window still open? |
| Old screen still appears | The old copy is still running. Close the old window (or restart the PC), make sure the file is named exactly `pc_controller.py`, start again. The window should say `version 3` and the phone's Power tab should show `version 3`. |
| Address changed | After a router or PC restart the address can change. Run the program again and scan the new QR code. |
| "Python is not installed" | Install Python and tick "Add python.exe to PATH". |
| Screen tab shows an error | `pillow` did not install. In Command Prompt run `pip install pillow`. |
| QR code does not appear | In Command Prompt run `pip install segno`, or type the address shown in the black window into Safari. |
| Brightness slider greyed out | The screen does not support software brightness. On desktop monitors, turn on **DDC/CI** in the monitor's own menu. |
| Typing does not work in some windows | Windows blocks typing into programs running as administrator. Run the controller as administrator too. |
| `Ctrl+Alt+Del` does nothing | Windows does not allow it to be sent remotely. |
| Port already in use | Change `PORT` in the settings, or close whatever else uses it. |

## Limits

- Windows only. The phone can be any device with a browser; it is built for iPhone Safari.
- It cannot turn the PC on. The PC must be on and the controller running.
- Screen view shows the main monitor only.
- Shortcut keys assume a US keyboard layout. Plain typing works in any language.

## Requirements

- Windows 10 or 11
- Python 3.8 or newer
- PC and phone on the same Wi-Fi network
- Internet on the PC for the first run only (to install `segno` and `pillow`)
