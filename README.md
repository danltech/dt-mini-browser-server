# DT Mini Browser – Rendering Server

**The server component of DT Mini Browser (DanlTech).** It loads web pages in a real headless Chromium (JavaScript and CSS included), extracts the finished layout and sends a compact *display list* (gzip JSON) to the client app, which only has to draw it.

Client app: [dt-mini-browser-android](https://github.com/danltech/dt-mini-browser-android)

> 🇩🇪 **Language note:** The client UI is currently **German only**. The server speaks plain JSON, but code comments and log messages are partly German too. Translations are welcome!

> ⚠️ **Early version.** Not hardened for public internet use yet (see *Security*).

---

## Features

- Real Chromium rendering via [Playwright](https://playwright.dev/) (JS, CSS, images)
- Compact display list: rectangles, text lines, images, links, form fields
- Image proxy with downscaling and JPEG compression (saves bandwidth)
- Ad/tracker host blocking (DoubleClick, Google Analytics, Taboola, …)
- Session handling (max. 4 parallel sessions, idle timeout 10 min)
- Cookies/login state persisted per client ID
- File downloads (limit 80 MB)
- SSRF protection: private/internal addresses are blocked by default
- Search fallback: non-URL input goes to DuckDuckGo (HTML)

## Supported platforms

| Platform | Status |
|---|---|
| Linux (Debian/Ubuntu, Raspberry Pi OS 64-bit, …) | ✅ Supported |
| Windows 10 / 11 | ✅ Supported (see [Windows](#windows)) |
| macOS | ✅ Should work (same steps as Linux) |

The server is plain Python with Playwright, aiohttp and Pillow and contains no OS-specific code, so it runs wherever Playwright's Chromium runs.

## Requirements

- Python **3.9+** (on Windows: 64-bit, from [python.org](https://www.python.org/downloads/); tick **"Add python.exe to PATH"** during setup)
- Chromium via Playwright (installed in the steps below, ~150–300 MB)
- RAM: roughly 300–500 MB per active session; the default limit is 4 sessions

## Installation

### Linux / macOS

```bash
git clone https://github.com/danltech/dt-mini-browser-server.git
cd dt-mini-browser-server
python3 -m venv venv && source venv/bin/activate
pip install aiohttp pillow playwright
playwright install chromium
python server.py
```

On a fresh Linux server you may also need Chromium's system libraries:
`playwright install-deps chromium` (run with `sudo`).

### Windows

Use **PowerShell** (not the old cmd):

```powershell
git clone https://github.com/danltech/dt-mini-browser-server.git
cd dt-mini-browser-server
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install aiohttp pillow playwright
playwright install chromium
python server.py
```

If PowerShell refuses to activate the venv ("running scripts is disabled"), run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

No git? Download the repository as a ZIP from GitHub and unpack it instead.

The server listens on `0.0.0.0:8080` by default and logs the address to use:

- Real device (same Wi-Fi): `http://<your-LAN-IP>:8080`
- Android emulator: `http://10.0.2.2:8080` (also works when the host is Windows)

## Hosting on Windows

### 1. Find your PC's IP address

```powershell
ipconfig
```

Look for **IPv4 Address** of your Wi-Fi/Ethernet adapter, e.g. `192.168.1.50`. Enter `http://192.168.1.50:8080` in the app under *Server-Adresse*. Give the PC a fixed address (DHCP reservation in your router) so it doesn't change.

### 2. Allow the port in the Windows Firewall

On first start Windows usually asks whether Python may accept connections. Allow it for **private networks only**. Or add the rule manually (PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "DT Mini Browser Server" -Direction Inbound `
  -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private
```

Make sure your home network is set to **Private** (Settings → Network & Internet → Properties). Don't open the port for the *Public* profile.

### 3. Test it

From the PC: open `http://localhost:8080/ping` in a browser. From the phone (same Wi-Fi): open `http://<PC-IP>:8080/ping`. If that works, the app will too.

### 4. Change port or settings (optional)

```powershell
$env:MINI_PORT = "9090"
$env:MINI_LOCALE = "en-US"
python server.py
```

To keep the server local to the PC only, use `$env:MINI_HOST = "127.0.0.1"`.

### 5. Start automatically at login

Create a small `start-server.bat` next to `server.py`:

```bat
@echo off
cd /d %~dp0
call venv\Scripts\activate.bat
python server.py
```

Then either put a shortcut to it in `shell:startup` (Win+R), or use **Task Scheduler**: *Create Task* → trigger *At log on* → action *Start a program* → `start-server.bat`. For a real background service without a login, tools like [NSSM](https://nssm.cc/) can wrap the script as a Windows service.

### 6. Using it outside your home network

**Don't just forward port 8080 on your router.** The server has no authentication or encryption yet (see *Security*). Safer options:

- A VPN such as **Tailscale** or **WireGuard**: the phone then reaches the PC via the VPN address, with no open ports.
- A reverse proxy with HTTPS and authentication in front of the server.

### Windows notes

- Keep the PC from sleeping while the server should be reachable (Settings → System → Power).
- Antivirus/Defender may slow down the first Chromium start. This is normal.
- If you use WSL2 or Docker Desktop instead, follow the Linux steps inside WSL2; for access from the phone you may need port forwarding from Windows into WSL2.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `MINI_HOST` | `0.0.0.0` | Bind address |
| `MINI_PORT` | `8080` | Port |
| `MINI_CHROME` | *(Playwright's)* | Path to a custom Chromium binary |
| `MINI_LOCALE` | `de-AT` | Browser locale |
| `MINI_SHOT` | `1` | `0` = no reference screenshot (debug) |
| `MINI_ALLOW_PRIVATE` | off | `1` = allow internal/private addresses ⚠️ |

## API

All endpoints are `GET`. Responses are JSON ("page state"), except `/img`, `/download`, `/ping`.

| Endpoint | Purpose |
|---|---|
| `/ping` | Health check |
| `/load?url=&w=&h=&dpr=[&sid=]` | Load a page (creates a session if `sid` is missing) |
| `/tap?sid=&x=&y=` | Tap at page coordinates (CSS px) |
| `/type?sid=&t=[&enter=1]` | Type text into the focused field |
| `/back?sid=` | Navigate back |
| `/state?sid=` | Re-read the current state (e.g. after JS updates) |
| `/img?sid=&id=&tw=&q=` | Downscaled JPEG of an image |
| `/download?sid=&id=` | Fetch a file received from the page |

Display list items (coordinates in CSS px, colors = ARGB):

```
["r", x,y,w,h, argb, radius]                      filled rectangle
["t", x,y,w,h, text, argb, fontPx, flags]         text line
["i", x,y,w,h, imgId, mode [,clip]]               image
["a", x,y,w,h, href]                              link area
["f", x,y,w,h, kind, text, checked, isPlaceholder, argb, fontPx]   form field
```

## Security – please read

- ❗ There is **no authentication and no TLS** built in. Anyone who can reach the port can use your server as a browser (and see session data). Run it in your **home network only**, or put it behind a reverse proxy with HTTPS and authentication (e.g. Caddy/nginx + basic auth, or a VPN such as WireGuard/Tailscale).
- The server sees all traffic of its users. Only run it for people who trust you, and tell them so.
- Don't set `MINI_ALLOW_PRIVATE=1` on a server exposed to others – it disables the internal-network protection.
- Session state files are written to `states/`, downloads to `downloads/`, debug output to `debug/`. They may contain cookies and personal data, so don't commit them (see `.gitignore`).

## Roadmap

- [ ] Token-based authentication
- [ ] Docker image
- [ ] Configurable session limit
- [ ] English log messages and comments

## Contributing

Issues and pull requests are welcome.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE) © DanlTech.

**What this means in short:** you may use, modify and self-host the server freely. If you modify it and let others use it over a network (e.g. as a public service), you must offer those users the source code of your modified version (AGPL §13). The Android client is licensed separately under Apache-2.0.

Third-party components (Playwright, aiohttp, Pillow, Chromium) have their own licenses.

## Disclaimer

DT Mini Browser is an independent project. It is **not affiliated with, endorsed by, or sponsored by** Opera Software or any other browser vendor. All trademarks belong to their respective owners.
