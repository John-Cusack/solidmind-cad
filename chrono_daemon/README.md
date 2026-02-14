# Chrono Daemon

Standalone C++ TCP server for multibody dynamics simulation via [Project Chrono](https://projectchrono.org/).

Mirrors the FreeCAD addon architecture: separate process, JSON protocol, TCP socket on `localhost:9877`.

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

```bash
./build/chrono_daemon              # Default: localhost:9877
./build/chrono_daemon --port 9878  # Custom port
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
