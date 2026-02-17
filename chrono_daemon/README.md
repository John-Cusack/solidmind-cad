# Chrono Daemon

Standalone C++ TCP server for multibody dynamics simulation via [Project Chrono](https://projectchrono.org/).

Mirrors the FreeCAD addon architecture: separate process, JSON protocol, TCP socket on `localhost:9877`.

**This is entirely optional.** Tier 1 (analytical) and Tier 2 (kinematic) validation work without it.

## Prerequisites

- GCC 11+ or Clang 14+
- CMake 3.16+
- Eigen3: `sudo apt install libeigen3-dev`
- Project Chrono (MBS module only)

## Build Chrono from Source

```bash
git clone https://github.com/projectchrono/chrono.git
cd chrono && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release \
         -DENABLE_MODULE_IRRLICHT=OFF \
         -DENABLE_MODULE_VEHICLE=OFF
make -j$(nproc)
```

## Build the Daemon

```bash
cd chrono_daemon
mkdir build && cd build
cmake .. -DChrono_DIR=/path/to/chrono/build
make
```

## Run

The wrapper script sets up `LD_LIBRARY_PATH` automatically:

```bash
# Set CHRONO_LIB_DIR if Chrono is not in /usr/local/lib
export CHRONO_LIB_DIR=~/chrono/install/lib

chrono_daemon/run.sh              # Default: localhost:9877
chrono_daemon/run.sh --port 9878  # Custom port
```

Or run the binary directly:

```bash
LD_LIBRARY_PATH=/path/to/chrono/lib ./build/chrono_daemon
```

## Systemd User Service (optional)

For auto-start on login:

```bash
# Edit chrono-daemon.service to set correct paths, then:
chrono_daemon/install-service.sh

systemctl --user start chrono-daemon    # start now
systemctl --user enable chrono-daemon   # auto-start on login
journalctl --user -u chrono-daemon -f   # view logs
```

## Protocol

Newline-delimited JSON (same as FreeCAD addon).

### Commands

```json
{"cmd": "ping", "args": {}}
{"cmd": "simulate", "args": {"mechanism": {...}, "duration_s": 1.0, "dt_s": 0.001, "output_interval": 0.01}}
{"cmd": "shutdown", "args": {}}
```

### Responses

```json
{"ok": true, "result": {"pong": true}}
{"ok": true, "result": {"time_series": [...], "summary": {...}}}
{"ok": false, "error": "error message"}
```
