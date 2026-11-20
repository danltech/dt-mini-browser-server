# DT Mini Browser – Server

**Server-side rendering browser backend.** Loads pages in a real Chromium (Playwright, JavaScript and CSS included) and sends a compact display list to a thin client such as [DT Mini Browser for Android](https://github.com/danltech/dt-mini-browser-android).

> Status: **Beta 1.0**. Expect rough edges.

## Quick start (Docker)

```bash
git clone https://github.com/danltech/dt-mini-browser-server.git
cd dt-mini-browser-server
# edit docker-compose.yml: set MINI_PUBLIC_HOST to your LAN IP / domain
docker compose up -d
```

On first start the server creates a token, a self-signed certificate and a setup QR code in `./minifiles/` (`qr-setup.png`, `setup.txt`). Scan the QR in the app, or paste `setup.txt`. Check: `https://<host>:8080/ping`.

Images are published for `linux/amd64` and `linux/arm64` at `ghcr.io/danltech/dt-mini-browser-server`.

## Run without Docker

Requires Python 3.10+ and Chromium.

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python server2.py
```

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `MINI_HOST` | `0.0.0.0` | Bind address |
| `MINI_PORT` | `8080` | Port |
| `MINI_CHROME` | *(Playwright's)* | Path to a Chromium binary |
| `MINI_LOCALE` | `en-US` | Browser locale |
| `MINI_FALLBACK_LANG` | `en` | Fallback language for server messages |
| `MINI_SHOT` | `1` | `0` = no reference screenshot (debug) |
| `MINI_SETUP` | `1` | Auto-generate token/certificate/QR if missing |
| `MINI_SSL` | `0` | `1` = HTTPS (self-signed certificate) |
| `MINI_TOKEN` | *(generated)* | Auth token (also in `minifiles/token.txt`) |
| `MINI_FILES` | `minifiles` | Folder for token, certificate, QR |
| `MINI_PUBLIC_HOST` / `MINI_PUBLIC_URL` | – | Address used in the setup QR (set this in Docker) |
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

- Use a **token** and **HTTPS** (`MINI_SETUP=1`, `MINI_SSL=1`) and keep the server in your home network, or reach it through a VPN (WireGuard/Tailscale) or an HTTPS reverse proxy. Do not simply open the port to the internet.
- The server sees all traffic of its users. Only run it for people who trust you.
- Never set `MINI_ALLOW_PRIVATE=1` on a server others can reach; it disables the internal-network protection.
- `minifiles/` (token, key), `states/`, `downloads/` and `debug/` contain secrets or personal data and must not be committed (see `.gitignore`).

## Translations

Server messages live in `i18n.py` (en, de, es, fr, it, tr, ru, ja, zh). Pull requests for more languages are welcome.

## Contributing

Issues and pull requests are welcome.

## License

[GNU AGPL v3.0](LICENSE) © DanlTech. If you modify the server and let others use it over a network, you must offer them the source of your version (AGPL §13). The Android client is licensed separately (Apache-2.0). Third-party components (Playwright, aiohttp, Pillow, Chromium) have their own licenses.

## Disclaimer

DT Mini Browser is an independent project and is not affiliated with, endorsed by, or sponsored by Opera Software or any other browser vendor. All trademarks belong to their respective owners.
