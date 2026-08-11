# sdr-docker

[![CI](https://github.com/HubbleNetwork/sdr-docker/actions/workflows/ci.yml/badge.svg)](https://github.com/HubbleNetwork/sdr-docker/actions/workflows/ci.yml)
[![Docker](https://github.com/HubbleNetwork/sdr-docker/actions/workflows/docker.yml/badge.svg)](https://github.com/HubbleNetwork/sdr-docker/actions/workflows/docker.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Live rolling spectrogram and packet decoder for SDR devices.  Streams IQ data,
displays a real-time spectrogram, decodes packets, and supports full-duplex TX —
all served as a web dashboard on port **8050**.

Uses [hubble-satnet-decoder](https://github.com/hubblenetwork/hubble-satnet-decoder) for packet
detection and decoding.

## Supported SDR devices

| Device | Interface | Notes |
|--------|-----------|-------|
| **[ADALM-PLUTO (PlutoSDR)](https://www.analog.com/en/resources/evaluation-hardware-and-software/evaluation-boards-kits/adalm-pluto.html#eb-overview)** | Ethernet (`ip:192.168.2.1`) or USB | Default. USB on Mac requires NCM firmware (see below) |
| **[Nuand bladeRF 2.0 Micro A4](https://www.nuand.com/product/bladerf-xa4/)** | USB | Set `SDR_TYPE=bladerf` |

All SDR hardware is accessed through a **single code path**: GNU Radio's
`gr-soapy` block, which wraps SoapySDR.  Adding support for a new device
(RTL-SDR, HackRF, LimeSDR, USRP, …) requires only a SoapySDR module for
that device — zero application code changes.

## Architecture

| Component | Process / Thread | Description |
|-----------|-----------------|-------------|
| **SDR RX** | GNU Radio flowgraph (C++ threads) | `soapy.source` → custom sink that writes into a 2 s shared-memory circular buffer |
| **Processor** | separate OS process | Every 0.5 s: compute spectrogram chunk, render 10 s image, decode. Runs in its own process to avoid GIL contention with the RX thread |
| **Flask server** | main process | Serves web dashboard + JSON API on port 8050 |

### Data flow

```
                      Main Process                          Processor Process
                      ────────────                          ─────────────────
SDR ──gr-soapy──> BufferSink ──> shared-memory IQ buffer ──> 0.5 s chunk ──> spectrogram
                                   (2 s circular)           1.0 s chunk ──> detection + decode
                                                                              │
                                                                         result_queue
                                                                              │
                  Flask (/api/status, /api/packets) <── drain thread <────────┘
```

### Project structure

```
src/stream_web/
├── config.py          # SDR / display constants (protocol constants via hubble-satnet-decoder)
├── gnuradio_rx.py     # GNU Radio RX flowgraph: soapy.source → BufferSink
├── gnuradio_tx.py     # GNU Radio TX flowgraph: soapy.sink (tone / packet file)
├── sdr.py             # Re-exports rx_loop
├── spectrogram.py     # Spectrogram image rendering (computation via hubble-satnet-decoder)
├── processor.py       # Processing loop (runs in separate OS process)
├── app.py             # Flask app, RX/TX routes, API endpoints, orchestration
├── templates/
│   └── index.html     # Dashboard HTML
└── static/
    └── style.css      # Dashboard CSS
```

## Dependencies

The application has two layers of dependencies:

| Layer | What | How to install |
|-------|------|----------------|
| **Python packages** (pip) | hubble-satnet-decoder, flask, numpy, scipy, matplotlib, Pillow | `pip install -e .` |
| **System libraries** (apt / brew / source) | GNU Radio >= 3.9, SoapySDR, per-device SoapySDR modules, device libraries | See platform-specific sections below |

GNU Radio and SoapySDR ship Python bindings that are installed system-wide
(not via pip).  To make them visible inside a virtualenv, always create the
venv with `--system-site-packages`:

```shell
python3 -m venv --system-site-packages .venv
```

The **Dockerfile** serves as the canonical reference for all system-level
dependencies and their build steps.

### Per-SDR system dependencies

| SDR | Device library | SoapySDR module | Notes |
|-----|---------------|-----------------|-------|
| PlutoSDR | libiio (>= 0.23), libad9361-iio | [SoapyPlutoSDR](https://github.com/pothosware/SoapyPlutoSDR) (build from source) | `apt install` on Linux; build both libs from source on macOS |
| bladeRF 2.0 | libbladerf | [SoapyBladeRF](https://github.com/pothosware/SoapyBladeRF) (build from source) | bladeRF firmware >= 2.6.0 required for FPGA v0.16.0 |

---

## Quick start

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SDR_TYPE` | `pluto` | SDR backend: `pluto` or `bladerf` |
| `PLUTO_URI` | `ip:192.168.2.1` | PlutoSDR connection URI (e.g. `ip:192.168.2.1` or `usb:`) |
| `BLADERF_SERIAL` | *(empty)* | Optional bladeRF serial number for multi-device setups |

---

## Setup — Docker on Linux

Docker is the recommended way to run on Linux.  It works for **both SDR
devices**, over **Ethernet or USB**.

### 1. Install Docker

Official instructions: <https://docs.docker.com/engine/install/>

Quick install with the convenience script:

```shell
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh ./get-docker.sh
sudo usermod -aG docker $USER
```

Log out and back in, then verify:

```shell
groups | grep docker
```

### 2. Clone and build

```shell
git clone https://github.com/HubbleNetwork/sdr-docker.git
cd sdr-docker/
docker build -t sdr-docker .
```

> **Non-x86 architectures:** download the correct libiio `.deb` from
> [libiio releases](https://github.com/analogdevicesinc/libiio/releases/tag/v0.26)
> and update the `wget` line in the [Dockerfile](./Dockerfile).

### 3. Run

#### PlutoSDR over Ethernet (default)

No special flags needed — the container reaches Pluto at `192.168.2.1` via the
host network stack:

```shell
docker run --restart unless-stopped -d -p 8050:8050 sdr-docker
```

Or use the pre-built image from GitHub Container Registry:

```shell
docker run --restart unless-stopped -d -p 8050:8050 ghcr.io/hubblenetwork/sdr-docker:latest
```

To use a different Pluto IP:

```shell
docker run --restart unless-stopped -d -p 8050:8050 \
  -e PLUTO_URI=ip:192.168.3.1 sdr-docker
```

#### PlutoSDR over USB

Pass the USB bus into the container:

```shell
docker run --restart unless-stopped -d -p 8050:8050 \
  --privileged \
  -e PLUTO_URI=usb: \
  sdr-docker
```

Or, for tighter security, map only `/dev/bus/usb`:

```shell
docker run --restart unless-stopped -d -p 8050:8050 \
  --device=/dev/bus/usb \
  -e PLUTO_URI=usb: \
  sdr-docker
```

#### bladeRF Micro A4 (USB)

```shell
docker run --restart unless-stopped -d -p 8050:8050 \
  --privileged \
  -e SDR_TYPE=bladerf \
  sdr-docker
```

> The container's entrypoint automatically loads the bladeRF FPGA bitstream
> before starting the app — no manual `bladeRF-cli` step needed.

> **Why `--restart unless-stopped`?**  See
> [Connection recovery](#connection-recovery-auto-restart) below.

### 4. Docker Compose (development)

```shell
docker build -t sdr-docker .

# PlutoSDR over Ethernet (default):
docker compose up

# PlutoSDR over USB:
SDR_TYPE=pluto PLUTO_URI=usb: docker compose up

# bladeRF:
SDR_TYPE=bladerf docker compose up
```

> **USB passthrough with Compose:** uncomment the `privileged: true` or
> `devices:` section in [`compose.yml`](./compose.yml).

### 5. Open the dashboard

Navigate to <http://localhost:8050> in a browser.

### 6. Stop

```shell
docker ps
docker kill <container_id>
```

---

## Setup — Native on macOS

Docker Desktop for Mac runs Linux inside a VM, which makes USB device
passthrough unreliable.  **Running natively is recommended on macOS** for both
PlutoSDR (USB) and bladeRF.

### Prerequisites

Install GNU Radio and base SDR support via Homebrew:

```shell
brew install gnuradio libusb cmake
```

This installs GNU Radio 3.10+ with gr-soapy (the unified SDR backend) and
SoapySDR.  Both are linked to the Homebrew Python (currently 3.14).

**For PlutoSDR**, build libiio, libad9361-iio, and SoapyPlutoSDR from source
(none are available as Homebrew formulae):

```shell
# 1. Build libiio from source
#    -DOSX_FRAMEWORK=OFF is critical — without it cmake produces a .framework
#    bundle that requires root to install and causes rpath issues downstream.
git clone --depth 1 --branch v0.25 https://github.com/analogdevicesinc/libiio.git
cd libiio && mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/opt/homebrew \
         -DWITH_TESTS=OFF -DWITH_SERIAL_BACKEND=OFF \
         -DOSX_PACKAGE=OFF -DOSX_FRAMEWORK=OFF
make -j$(sysctl -n hw.ncpu) && make install
cd ../..

# 2. Build libad9361-iio (AD9361 transceiver support library)
#    Same -DOSX_FRAMEWORK=OFF requirement as libiio.
git clone --depth 1 https://github.com/analogdevicesinc/libad9361-iio.git
cd libad9361-iio && mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/opt/homebrew \
         -DOSX_PACKAGE=OFF -DOSX_FRAMEWORK=OFF
make -j$(sysctl -n hw.ncpu) && make install
cd ../..

# 3. Build SoapyPlutoSDR module
git clone --depth 1 https://github.com/pothosware/SoapyPlutoSDR.git
cd SoapyPlutoSDR && mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/opt/homebrew
make -j$(sysctl -n hw.ncpu) && make install
cd ../..

# 4. Fix dynamic library paths (macOS rpath issue)
#    Even with -DOSX_FRAMEWORK=OFF, the built binaries sometimes end up with
#    framework-style references (@rpath/iio.framework/...) instead of dylib
#    references.  These install_name_tool commands patch them to use the
#    correct dylib names and add /opt/homebrew/lib to the rpath search.
for lib in \
  /opt/homebrew/lib/libad9361.0.2.dylib \
  /opt/homebrew/lib/SoapySDR/modules0.8/libPlutoSDRSupport.so; do
  install_name_tool -change \
    "@rpath/iio.framework/Versions/0.25/iio" \
    "@rpath/libiio.0.dylib" "$lib" 2>/dev/null
  install_name_tool -add_rpath /opt/homebrew/lib "$lib" 2>/dev/null
done

# 5. Clean up source trees
rm -rf libiio libad9361-iio SoapyPlutoSDR

# Verify:
SoapySDRUtil --find="driver=plutosdr"
```

> **Why not `sudo make install`?** On Apple Silicon Macs, `/opt/homebrew` is
> owned by the user, so `sudo` is not needed.  If you see permission errors,
> prefix the `make install` commands with `sudo`.

**For bladeRF Micro A4**, install libbladerf and build SoapyBladeRF:

```shell
brew install libbladerf

# Build SoapyBladeRF module
git clone --depth 1 https://github.com/pothosware/SoapyBladeRF.git
cd SoapyBladeRF && mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/opt/homebrew
make -j$(sysctl -n hw.ncpu) && make install
cd ../.. && rm -rf SoapyBladeRF

# Flash FPGA to auto-load (one-time — persists across power cycles)
wget https://www.nuand.com/fpga/hostedxA4-latest.rbf -O /tmp/hostedxA4.rbf
bladeRF-cli -L /tmp/hostedxA4.rbf

# Verify:
SoapySDRUtil --find="driver=bladerf"
```

**Verifying all modules loaded:**

```shell
SoapySDRUtil --info
# Should list "Available factories... bladerf, plutosdr, rtlsdr"
# If a module shows "dlopen() failed", check the error for missing libraries.
```

### Install Python dependencies

Use a venv with `--system-site-packages` so the Homebrew-installed GNU Radio
and SoapySDR Python bindings are visible inside the venv:

```shell
cd sdr-docker/
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .
```

> **Why `--system-site-packages`?** GNU Radio's Python bindings are installed
> system-wide by Homebrew (into Python 3.14's `site-packages`).  A standard
> venv isolates from system packages and would not see them.  The
> `--system-site-packages` flag allows the venv to fall through to the system
> packages for anything not installed locally.

### Run

**PlutoSDR over USB:**

```shell
PLUTO_URI=usb: python3 run_stream.py
```

**PlutoSDR over Ethernet** (requires NCM firmware — see troubleshooting below):

```shell
python3 run_stream.py
```

**bladeRF Micro A4:**

```shell
SDR_TYPE=bladerf python3 run_stream.py
```

Open <http://localhost:8050>.

---

## Setup — Native on Linux

Running natively (without Docker) on Linux follows the same pattern.

### Prerequisites

```shell
sudo apt update
sudo apt install -y python3-pip python3-venv git

# GNU Radio (with gr-soapy built in) — available in Ubuntu 22.04+ repos.
# For older distros, add the PPA: sudo add-apt-repository -y ppa:gnuradio/gnuradio-releases
sudo apt install -y gnuradio

# SoapySDR runtime and development files
sudo apt install -y libsoapysdr-dev python3-soapysdr

# Build tools (needed to compile SoapySDR device modules from source)
sudo apt install -y cmake g++
```

**PlutoSDR support** (libiio and libad9361 are available as system packages on Linux):

```shell
sudo apt install -y libiio-dev libiio-utils libad9361-dev

git clone --depth 1 https://github.com/pothosware/SoapyPlutoSDR.git
cd SoapyPlutoSDR && mkdir build && cd build
cmake .. && make -j$(nproc) && sudo make install
cd ../.. && rm -rf SoapyPlutoSDR
sudo ldconfig
```

If using **PlutoSDR over USB**, add a udev rule for non-root access:

```shell
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0456", ATTR{idProduct}=="b673", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/53-plutosdr.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Verify:

```shell
# PlutoSDR reachable (should list Analog Devices PlutoSDR):
iio_info -s

# SoapySDR sees the module:
SoapySDRUtil --find="driver=plutosdr"
```

**bladeRF support** (optional):

```shell
sudo apt install -y libbladerf-dev libbladerf2 bladerf

git clone --depth 1 https://github.com/pothosware/SoapyBladeRF.git
cd SoapyBladeRF && mkdir build && cd build
cmake .. && make -j$(nproc) && sudo make install
cd ../.. && rm -rf SoapyBladeRF
sudo ldconfig
```

**USB permissions** — add a udev rule so the bladeRF is accessible without
root:

```shell
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2cf0", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/53-bladerf.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Unplug and re-plug the bladeRF after applying.

**FPGA bitstream** — the bladeRF 2.0 needs an FPGA image loaded at each
power-on.  Flash it to auto-load so this is handled permanently:

```shell
wget https://www.nuand.com/fpga/hostedxA4-latest.rbf -O /tmp/hostedxA4.rbf
bladeRF-cli -L /tmp/hostedxA4.rbf
```

> Capital `-L` writes the FPGA image to flash so it loads automatically on
> every power-on.  If you skip this step, the app will fail on first connect
> and leave the bladeRF in a bad state requiring a power cycle.

Verify with `SoapySDRUtil --info` — you should see `plutosdr` and/or
`bladerf` listed under "Available factories".

### Install and run

```shell
cd sdr-docker/

# --system-site-packages is required so the venv can see
# GNU Radio and SoapySDR Python bindings installed by apt
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .
```

> **NumPy constraint:** `pyproject.toml` pins `numpy>=1.26,<2`.  The GNU Radio
> packages from apt (and Homebrew) are compiled against the NumPy 1.x ABI.
> If NumPy 2.x is installed, `import gnuradio` will fail with
> `_ARRAY_API not found`.

> **GNU Radio vmcircbuf warning:** on native Linux you may see
> `vmcircbuf_prefs::get :error: …/vmcircbuf_default_factory: No such file`.
> This is harmless (GNU Radio falls back automatically), but to silence it:
> ```shell
> mkdir -p ~/.gnuradio/prefs
> echo "shm_open" > ~/.gnuradio/prefs/vmcircbuf_default_factory
> ```

```shell
# PlutoSDR (Ethernet, default):
python3 run_stream.py

# PlutoSDR (USB):
PLUTO_URI=usb: python3 run_stream.py

# bladeRF Micro A4:
SDR_TYPE=bladerf python3 run_stream.py
```

---

## Configuration

All tuneable parameters live in
[`src/stream_web/config.py`](src/stream_web/config.py).  Key settings:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SDR_TYPE` | `pluto` | SDR backend (`pluto` or `bladerf`) |
| `PLUTO_URI` | `ip:192.168.2.1` | PlutoSDR connection URI |
| `CENTER_FREQ_HZ` | 2.482754875 GHz | Centre frequency |
| `SAMPLE_RATE` | 781 250 Hz | ADC sample rate |
| `RX_INITIAL_GAIN_DB` | 20 | Initial RX gain (adjustable from the UI) |
| `FLASK_PORT` | 8050 | Web server port |
| `SPEC_DURATION_S` | 10.0 | Rolling spectrogram window |
| `DECODE_INTERVAL_S` | 0.5 | Decode cycle interval |

## Web dashboard

The dashboard auto-refreshes every 500 ms and provides:

- **Live spectrogram** — 10 s rolling window with coloured detection boxes
  (orange = PHY v-1, red = PHY v1).
- **Decodes tab** — per-device summary: PHY version, device ID, chipset, RSSI,
  frequency delta, last 10 sequence numbers, last-seen timestamp.
- **Statistics tab** — per-chipset decode success rates.
- **Signal Viewer** — view time-domain + spectrogram plots per device or per
  chipset. Includes symbol timing, frequency labels, and full decode diagnostics.
  Can capture failures for a specific chipset to aid debugging.
- **Gain control** — adjust RX gain from the browser.
- **LO control** — adjust LO frequency in 1 kHz increments.
- **TX Control** — transmit a CW tone or play back IQ files, with attenuation
  control.  Full-duplex (TX runs simultaneously with RX).

### Packet feed API (`/api/packets`)

A poll-and-drain JSONL endpoint for external agents / scripts.  Each call
returns all packets decoded since the last call, one JSON object per line,
then clears the buffer.

**Response format** (`application/x-ndjson`):

```json
{"device_id": "0xBBAABB01", "seq_num": 155, "device_type": "silabs", "timestamp": 1709571234.567, "rssi_dB": -30.6, "channel_num": 13, "freq_offset_hz": 20902.0, "payload_b64": "AQIDBAUGBwgJCgsMDQ=="}
{"device_id": "0xBBAABB01", "seq_num": 156, "device_type": "silabs", "timestamp": 1709571235.102, "rssi_dB": -31.2, "channel_num": 10, "freq_offset_hz": 20910.5, "payload_b64": "AQIDBAUGBwgJCgsMDQ=="}
```

**Fields:**

| Field | Description |
|-------|-------------|
| `device_id` | Network ID as hex string |
| `seq_num` | Packet sequence number |
| `device_type` | Chipset name (e.g. `silabs`, `nordic`, `ti`) |
| `timestamp` | Unix epoch (float, seconds) |
| `rssi_dB` | Signal energy in dBFS |
| `channel_num` | Frequency channel from the decoded header |
| `freq_offset_hz` | Measured frequency offset from nominal channel center (Hz) |
| `payload_b64` | Packet payload as base64-encoded string (empty if no payload) |

**CLI examples:**

```bash
# One-shot: fetch all new packets
curl -s http://localhost:8050/api/packets

# Continuous polling (every 2 seconds), append to JSONL file
while true; do curl -s http://localhost:8050/api/packets >> decodes.jsonl; sleep 2; done

# Pretty-print with jq
curl -s http://localhost:8050/api/packets | jq .

# Filter for a specific device
curl -s http://localhost:8050/api/packets | jq 'select(.device_id == "0xBBAABB01")'

# Monitor in real-time with watch
watch -n 2 'curl -s http://localhost:8050/api/packets | jq .'
```

### TX API

Full-duplex TX runs simultaneously with RX.  All TX endpoints live under `/api/tx/`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tx/start` | POST | Start transmitting. Body: `{"mode":"tone"}` for CW carrier, or `{"mode":"packet","file":"<name>","repeat":true}` for IQ file playback |
| `/api/tx/stop` | POST | Stop transmitting |
| `/api/tx/status` | GET | Current TX state (running, mode, freq, attenuation) |
| `/api/tx/freq` | GET/POST | Get or set TX frequency. POST body: `{"freq_hz": 2482440375}` |
| `/api/tx/attn` | GET/POST | Get or set TX attenuation. POST body: `{"attn_db": 0}` (0 = max power) |
| `/api/tx/files` | GET | List available TX IQ files with name, size, and SHA256 hash |
| `/api/tx/files` | POST | Upload an IQ binary file (multipart form, max 1 GB). Returns `{name, size, sha256}` |
| `/api/tx/files/<name>` | DELETE | Delete a TX file |

**CLI examples:**

```bash
# Start a CW tone at max power
curl -X POST http://localhost:8050/api/tx/start \
  -H 'Content-Type: application/json' -d '{"mode":"tone"}'

# Set attenuation to 0 dB (max power)
curl -X POST http://localhost:8050/api/tx/attn \
  -H 'Content-Type: application/json' -d '{"attn_db": 0}'

# Upload an IQ file for TX
curl -X POST http://localhost:8050/api/tx/files -F "file=@my_waveform.bin"

# List available TX files
curl -s http://localhost:8050/api/tx/files | jq .

# Start packet TX from an uploaded file
curl -X POST http://localhost:8050/api/tx/start \
  -H 'Content-Type: application/json' \
  -d '{"mode":"packet","file":"my_waveform.bin"}'

# Stop TX
curl -X POST http://localhost:8050/api/tx/stop

# Delete a TX file
curl -X DELETE http://localhost:8050/api/tx/files/my_waveform.bin
```

---

## Connection recovery (auto-restart)

The SDR RX thread monitors liveness by tracking the last time IQ samples
arrived.  If no samples are received for **5 seconds**, the connection is
considered lost.

**Why the process exits instead of reconnecting in-process:**  PlutoSDR uses
libiio for its network/USB backend.  Once a libiio pipe breaks (error `-32` /
`EPIPE`), the library's internal state is corrupted and creating a new
`iio_context` in the same process inherits the broken state.  In-process
reconnection does not work — a fresh process is required.

When a connection drop is detected, the process exits with **code 3**.
Docker's `--restart unless-stopped` policy (or `on-failure`) automatically
brings up a clean container, which establishes a fresh libiio context and
reconnects.  A typical recovery cycle takes **5–10 seconds**.

**Initial connection retries still work in-process** — if the SDR is not yet
available at startup (e.g. device still booting), the code retries every few
seconds until the device appears.  The exit-on-loss behaviour only applies
after a successful streaming session drops.

For native (non-Docker) usage, wrap the command in a process supervisor or a
simple restart loop:

```shell
while true; do
  python3 run_stream.py
  echo "[supervisor] Process exited ($?), restarting in 5s..."
  sleep 5
done
```

---

## Troubleshooting

### PlutoSDR USB on macOS — NCM firmware

The PlutoSDR ships with RNDIS-mode USB networking, which macOS does not support
natively.  There are two options:

**Option A — Switch Pluto to NCM mode (recommended for Ethernet-over-USB):**

1. Plug in the PlutoSDR.  It appears as a USB mass-storage drive (`PlutoSDR`).
2. Open `config.txt` on the drive and change:
   ```
   usb_ethernet_mode = ncm
   ```
3. Eject the drive and power-cycle the Pluto.
4. After reboot, a new network interface should appear and the Pluto will be
   reachable at `192.168.2.1`.

> You may also need to update the PlutoSDR firmware to a version that supports
> NCM.  See the
> [ADI firmware update guide](https://wiki.analog.com/university/tools/pluto/users/firmware).

**Option B — Use the IIO USB backend (no network needed):**

Run with `PLUTO_URI=usb:` — libiio communicates over raw USB, bypassing
Ethernet entirely.  This is the simplest option on macOS:

```shell
PLUTO_URI=usb: python3 run_stream.py
```

### bladeRF not detected

```shell
# Check that libbladeRF sees the device:
bladeRF-cli -p

# Check that SoapySDR sees the device:
SoapySDRUtil --find="driver=bladerf"

# If SoapySDRUtil shows nothing, verify the SoapyBladeRF module is installed:
SoapySDRUtil --info
# Look for "bladerf" in the list of available modules.
```

### bladeRF FPGA not loaded

The bladeRF 2.0 requires an FPGA bitstream.  If the FPGA has not been
flashed to auto-load (see setup sections above), it must be loaded manually
at each power-on.  Without a loaded FPGA, the app will fail on the first
connection attempt and leave the bladeRF in a bad state that requires a
**USB power cycle** (unplug and re-plug).

**Recommended fix** — flash once so it auto-loads permanently:

```shell
wget https://www.nuand.com/fpga/hostedxA4-latest.rbf -O /tmp/hostedxA4.rbf
bladeRF-cli -L /tmp/hostedxA4.rbf
```

If you only need to load for the current session (lowercase `-l`):

```shell
bladeRF-cli -l /tmp/hostedxA4.rbf
```

### bladeRF firmware version

FPGA v0.16.0 requires firmware **>= 2.6.0**.  If you see errors like
`FPGA v0.16.0 requires firmware v2.6.0+`, update the firmware:

```shell
# Download latest firmware:
wget https://www.nuand.com/fx3/bladeRF_fw_latest.img -O /tmp/bladeRF_fw.img

# Flash it:
bladeRF-cli -f /tmp/bladeRF_fw.img

# IMPORTANT: power-cycle the bladeRF (unplug & re-plug) after flashing.
# Then reload the FPGA:
bladeRF-cli -l /tmp/hostedxA4.rbf
```

Check versions with `bladeRF-cli -e version`.

### bladeRF streaming instability on macOS / Apple Silicon

The bladeRF 2.0 has a [known issue](https://github.com/Nuand/bladeRF/issues/977)
with USB streaming stability on ARM-based platforms (Raspberry Pi 5, Apple
Silicon Macs).  Symptoms include `NIOS II response: Operation timed out` and
`bladerf_sync_rx() returned -1` errors, sometimes after only a few successful
reads.

**Mitigations:**

- **Use GNU Radio** (this project's default) rather than raw SoapySDR calls.
  GNU Radio's C++ streaming threads keep the USB transfer loop tight and
  unblocked, which significantly improves stability.
- **Connect directly** to the Mac — avoid USB hubs.
- **Power-cycle** the bladeRF if it gets into a bad state (streaming errors
  persist until the device is fully unplugged and reconnected).
- For reliable bladeRF operation, **Linux x86** is recommended.

### Docker USB passthrough on macOS

Docker Desktop for Mac runs Linux inside a lightweight VM.  USB devices on the
Mac host are **not** visible inside this VM, so `--privileged` and `--device`
flags do not help.

**Workaround options:**

1. **Run natively** (recommended) — see the macOS setup section above.
2. Use [OrbStack](https://orbstack.dev/) instead of Docker Desktop — it has
   experimental USB passthrough support.
3. For PlutoSDR only: use Ethernet mode (`ip:192.168.2.1`) which works fine
   through Docker on Mac, but requires the NCM firmware change described above.

### PlutoSDR "no device found in this context" on Linux

If `SoapySDRUtil --find="driver=plutosdr"` detects the device but the app
fails with `no device found in this context`, the likely cause is a **libiio
version mismatch** between the host library (e.g. 0.23 from Ubuntu repos) and
the PlutoSDR firmware (e.g. 0.26).

This has already been fixed in the code — the app uses `iio_create_context_from_uri()`
(via the SoapySDR `uri=` key) instead of `iio_create_network_context()` (the
`hostname=` key), which tolerates version differences.

If you still see this error, verify with:

```shell
# Should show the PlutoSDR and its firmware version:
iio_info -s

# Should return device details (not an error):
iio_info -u ip:192.168.2.1
```

### `ModuleNotFoundError: No module named 'gnuradio'` (macOS / Linux native)

GNU Radio's Python bindings are installed system-wide by the package manager
(Homebrew or apt), not via pip.  If your venv was created **without**
`--system-site-packages`, it cannot see them.

**Option A — Recreate the venv** (cleanest):

```shell
rm -rf .venv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .
```

**Option B — Add a `.pth` file** (if you want to keep your existing venv):

```shell
# macOS (Homebrew)
echo "$(brew --prefix gnuradio)/lib/python3.14/site-packages" \
  > .venv/lib/python3.14/site-packages/gnuradio-brew.pth

# Linux (apt)
echo "/usr/lib/python3/dist-packages" \
  > .venv/lib/python3.*/site-packages/gnuradio-apt.pth
```

Verify with:

```shell
source .venv/bin/activate
python3 -c "from gnuradio import gr, soapy; print('OK')"
```

### USB permissions on Linux

If the SDR is not detected as a non-root user, you likely missed the udev
rule during setup.  See the udev steps in the
[Native on Linux](#setup--native-on-linux) section above.  After applying
the rule, unplug and re-plug the device.
