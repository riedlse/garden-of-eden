# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Control software for a **Gardyn** hydroponic tower, running on a Raspberry Pi (Zero 2 W / Zero W)
inside the unit. It drives the hardware over GPIO and I2C: grow light (PWM), water pump (PWM),
ultrasonic water-level sensor, air temp/humidity (AM2320 on Gardyn 1.0/2.0, DHT20 on 3.0+), PCB
temperature (PCT2075), pump power monitor (INA219), two USB cameras (`fswebcam`), and a momentary
button.

Three control surfaces sit on **one driver layer** (`app/sensors/*`):
- **MQTT service** (`mqtt.py`) — long-running, Home Assistant auto-discovery; `mqtt.service`.
- **Flask REST API + built-in web UI** (`run.py` → `app.create_app`) — `garden-api.service`
  (waitress, `:5000`). The UI is a single self-contained page, `app/web/index.html`, served at `/`.
- **CLI** — every driver module runs standalone with argparse flags; the `light`/`water` shell
  wrappers (`bin/light.sh`, `bin/water.sh`) are what cron and the schedule feature call.

The v2 code is on `main` of `iot-root/garden-of-eden`. `README.md` and `docs/INSTALL.md` still say
it lives on a `v2-overhaul` branch of the `HeatherFlux` fork — that is stale. `docs/design.md`
has the architecture rationale; `docs/maintenance.md` is the ops runbook.

## Commands

### Off-Pi (dev laptop, CI)

The hardware libraries do not install off-Pi. `tests/_hwstub.py` (run by `tests/__init__.py`)
injects fake `board`/`busio`/`gpiozero`/`adafruit_*`/`pigpio`/`smbus`/`ina219` modules into
`sys.modules` — only when the real ones are absent — so the full suite runs on any machine.

```bash
python -m venv .venv-dev && .venv-dev/bin/pip install -r requirements-dev.txt
source .venv-dev/bin/activate

# CI gate — exactly what .github/workflows/ci.yml runs (Python 3.9 and 3.11)
ruff check . && black --check .
python -m unittest discover -t . -s tests -p 'test_*.py'

# One module / one test
python -m unittest tests.test_water -v
python -m unittest tests.test_schedule.<TestCase>.<test_name> -v
```

Use the `discover -t . -s tests` form or the dotted `tests.<module>` form: both import the `tests`
package first, which installs the stubs. Plain `python -m unittest` from the repo root imports
`app` before the stubs exist and fails with dozens of errors (the README's `python -m unittest`
only works on a Pi with the real libs).

Style: black, line length 100; ruff `E,F,I` with `E501`/`E402` ignored (`pyproject.toml`).
`E402` is ignored on purpose — simulator and test bootstraps must seed env vars / stubs before
importing `config` or `app`.

### Simulator (off-Pi manual testing)

```bash
python -m simulator.serve      # web UI + REST on http://localhost:5000 with fake hardware
python -m simulator.mqtt_sim   # runs mqtt.py via runpy against a local broker (docker compose up broker)
```

`simulator/fake_hardware.install()` injects *stateful* fakes (actuators remember values, the water
level oscillates through the low threshold) and must run before `app` is imported — unlike
`tests/_hwstub.py`, which is bare mocks. Both entry points keep state files under
`simulator/.sim/`, return a placeholder JPEG for cameras, and stub the crontab reader/writer so
schedules never touch the host. See `docs/simulator.md`.

### On the Pi

```bash
./bin/setup.sh [--dry-run] [--yes] [--verbose]   # installer; see "Deployment" below
source venv/bin/activate
python run.py                  # Flask debug server (dev only; the service runs waitress)
python mqtt.py                 # normally started by systemd
./bin/api-test.sh              # curls every REST endpoint on localhost:5000

python app/sensors/light/light.py [--on] [--off] [--brightness 0-100] [--ramp-minutes N]
python app/sensors/pump/pump.py  [--on] [--off] [--speed 0-100] [--factory-host H --factory-port P]
python app/sensors/distance/distance.py        # temperature / humidity / pcb_temp likewise
light <off|on|0-100> | light ramp <brightness> <minutes> | water <off|on|seconds>
garden-update                  # bin/update.sh: git pull --ff-only, pip install, restart both services
```

## Architecture

### Sensor module pattern (the core convention)
Every hardware sensor lives in `app/sensors/<name>/` with the same shape:
- `<name>.py` — the driver: a class (`Light`, `Pump`, `Distance`, `TemperatureSensor`, …) or
  functions, plus an argparse `__main__` block. GPIO drivers accept a `pin_factory` argument.
- `routes.py` — a Flask `Blueprint`. It instantiates the driver **once at import** inside
  `try/except` (→ `None` on failure) and wraps each route with `check_sensor_guard`.
- `__init__.py`.

Not everything under `app/sensors/` is hardware: `grow`, `pods`, `schedule`, and `system` are
route-only modules backed by `app/lib/` (`grow.py`, `pods.py`) or their own logic module
(`schedule/schedule.py`); `camera/camera.py` shells out to `fswebcam`/`ffmpeg`.

`app/__init__.py` (`create_app`) applies CORS, registers optional API-key auth, and mounts the
blueprints: `/light`, `/pump`, `/distance`, `/temperature`, `/humidity`, `/pcb-temp`, `/camera`,
`/schedule`, `/grow`, `/pods`, `/system`, the web UI at `/`, and `/health`. Adding a sensor = a
new folder following the pattern + one `register_blueprint` line. The README's endpoint table is
incomplete — `grep -rn '\.route(' app/` is the source of truth (e.g. `POST /pump/run`,
`/camera/timelapse/<cam>`, `/pods`).

### Shared helpers (`app/lib/`)
- `hardware.py` — `get_pin_factory()` returns the **one** process-wide `PiGPIOFactory` (honors
  `PIGPIO_HOST`/`PIGPIO_PORT`; returns `None` off-Pi). `detect_model()` infers the Gardyn model
  from I2C presence / `SENSOR_TYPE`, short-circuited by `GARDYN_MODEL`.
- `lib.py` — `check_sensor_guard(sensor, name)`: 400 if the driver is `None`, 400 on
  `ValueError` (validation), pass-through for Flask `HTTPException`, **503** for anything else
  (hardware unavailable). `parse_level()` validates 0–100 request bodies.
- `logging_config.py` — `configure_logging()`; level from `LOG_LEVEL`. Each process passes its
  own file: `mqtt.py` → `mqtt.log`, `run.py` → `garden-api.log` (rotating 1 MB × 3).
- `persist.py` — `write_json_atomic()` (temp file + fsync + `os.replace`) used by every store
  below, so a power cut mid-write can't corrupt the SD card copy.
- `state.py` (actuator state, `~/.garden_state.json`), `grow.py` (grow cycle + reminders,
  `~/.garden_grow.json`), `pods.py` (per-pod plant names/symbols, `~/.garden_pods.json`, catalog
  in `plants.json`), `water.py` (`is_water_low`, `gallons_remaining` — pure functions).

### Configuration is centralized
All pins, I2C addresses, thresholds, file paths, and feature flags live in `config.py`
(env-driven via `python-dotenv`, hex-aware int parsing) and are documented in `.env-dist`.
Drivers default from `config.*` — don't hardcode. `config` reads the environment **at import**,
so anything that needs different values (simulator, tests) must set env vars before importing
`config`/`app`. `SENSOR_TYPE` unset makes the temperature/humidity drivers fail to init.

### Scheduling writes the Pi's crontab
`app/sensors/schedule/schedule.py` persists a per-weekday lights/pump schedule
(`~/.garden_schedule.json`), migrates legacy single-window shapes on load, and compiles it into
crontab lines tagged `# garden-of-eden` that invoke `/usr/local/bin/light` and
`/usr/local/bin/water`. Only lines with that marker are rewritten. Vacation mode swaps in a
reduced profile plus a nightly `bin/schedule-refresh.sh` entry so it auto-expires. The MQTT
service's `schedule/*/set` topics call the same `apply_schedule`.

### Pump safety cap
`MAX_PUMP_RUN_SECONDS` (default 300) is enforced independently in four places: the pump routes
(auto-off timer on `/on`, `/speed`, `/run`), `schedule.py` (`_pump_seconds` clamp), `mqtt.py`
(`_arm_pump_safety`), and `bin/water.sh` — which **hardcodes** `TIME_MAX=300` rather than reading
config. Change them together.

### mqtt.py specifics
- Reuses the **singleton drivers created by the route modules** (`from app.sensors.light.routes
  import light_control`, etc.). Constructing a second driver on the same pin raises
  `GPIOPinInUse` and crash-loops the service. Importing `mqtt` also constructs the `gpiozero.Button`
  at module scope.
- Subscribes to `BASE_TOPIC/#` and dispatches on the topic suffix inside `on_message`. Command
  topics: `light/command` (`ON`/`OFF`), `light/brightness/set`, `pump/command`, `pump/speed/set`,
  `*/get` triggers for each sensor, `water/low/cm/set`, `grow/stage/set`, `grow/start/set`, and the
  `schedule/{lights,pump,vacation}/…/set` family. Adding a controllable thing = a branch in
  `on_message` + an entity in `send_discovery_messages` + the expectation in `tests/test_discovery.py`.
- Home Assistant discovery (`send_discovery_messages`) reports `detect_model()` as the device
  model; availability/LWT on `BASE_TOPIC/availability`; WARNING+ log records are mirrored to
  `BASE_TOPIC/log` for an HA "Last Log" sensor (`MqttLogHandler`).
- Telemetry runs in six daemon threads (`while True: … sleep`), not timers: PCB temp, air temp,
  humidity (30 min each), water level (`WATER_CHECK_SECONDS`), camera images
  (`IMAGE_INTERVAL_SECONDS`, published retained), grow reminders.
- Physical button: single press → toggle light, double → toggle pump, hold → event only. All three
  publish `{"event_type": …}` to `BASE_TOPIC/button/event` (an HA `event` entity). Gotcha:
  `hold_time` (2 s) is longer than the double-press window (1 s), so a long press also fires the
  single-press light toggle first.
- Power-loss recovery: `restore_actuator_state` runs on connect (the `on_connect` callback is
  re-assigned *after* `client.connect()` but before `loop_forever()` — keep that ordering).
  `graceful_shutdown` turns the pump off on SIGTERM/SIGINT. State is persisted by the REST routes
  and the button toggles via `app.lib.state`; the MQTT command handlers currently do **not**
  persist, and `water/low/cm/set` only mutates a module global (lost on restart).
- **Service starts are destructive, and a dead broker makes them constant.** Constructing
  `Light`/`Pump` starts gpiozero PWM, which writes the pin LOW and sets duty 0 (verified by
  simulation against a fake pigpio). That happens at import in every start of `mqtt.service` and
  `garden-api.service`. `mqtt.py` uses a blocking `client.connect()`, so an unreachable broker
  kills the process and systemd (`Restart=always`, `RestartSec=5`, no start limit) relaunches it
  forever, zeroing the light and pump every few seconds and overriding whatever cron set. Keep
  `MQTT_BROKER=localhost` (the Pi's own mosquitto) until this is fixed; reach Home Assistant
  through a mosquitto bridge instead. Cron-driven changes are also never persisted, so
  `restore_actuator_state` cannot put back a light that cron turned on.
- Tests drive it offline: `tests/test_mqtt_control.py` and `tests/test_discovery.py` import
  `mqtt` under the stubs and call `on_message` / `send_discovery_messages` with a fake client.

### REST routes are a public contract
The built-in UI (`app/web/index.html`), `bin/api-test.sh`, and `tests/test_integration.py`
(which smoke-tests every GET route for 500s) consume these routes; changing one means updating
the UI page and `api-test.sh` too. Optional API-key auth (`GARDEN_API_KEY`, `X-API-Key` header)
exempts localhost, CORS preflight, `/`, `/health`, and `/static/*`.

A third-party Electron controller, `garden-of-eden-app` (checked out as a sibling at
`../garden-of-eden-app`; one static `index.html`, REST only, manual IP entry), predates v2 and
is already out of sync: it calls `/camera/timelapse/{status,start,stop,…}` routes that don't
exist here and can't parse the per-weekday `GET /schedule` shape. Its README says to replace
this repo's `run.py` with its `pi/run.py` — **don't** on a v2 install: that wrapper re-adds a
subset of what `create_app` already provides and drops `/grow`, `/pods`, `/system`, `/pump/run`,
and the schedule validation. The built-in web UI is a superset of the app.

## Deployment & packaging

- `bin/setup.sh` — one-shot Pi installer with `--dry-run`. Installs apt deps (pigpio, i2c-tools,
  fswebcam, ffmpeg, mosquitto, avahi, openssh), creates `venv/`, enables I2C, auto-detects
  `SENSOR_TYPE` (writes it to `.env`), enables SSH and mDNS (`GARDEN_HOSTNAME`, default `gardyn`),
  symlinks `light`/`water`/`garden-update`, installs camera udev rules, and installs systemd units:
  `mqtt.service`, `garden-api.service` (waitress), `garden-boot-indicator.service` (pulses the
  lights on power-on), and `garden-autoupdate.timer` (~03:30 nightly `bin/autoupdate.sh`, with a
  scoped sudoers rule). `bin/uninstall.sh` reverses it. It installs **no cron jobs**; the only
  crontab writer is the schedule feature. **Its last step (`verify_api`) runs `bin/api-test.sh`
  with output discarded, and that script drives real hardware**: light to 30 %, 0, full on, off;
  pump to 30 %, 0, `/pump/on` (**100 % duty**), off, then 30 % and 10 %, about a second each. So a
  plain install is not output-safe; nothing warns the user.
- setup.sh **regenerates the git-tracked** `services/etc/systemd/system/mqtt.service` with
  machine-specific paths, so a deployed Pi has a permanently dirty working tree there; keep that in
  mind if `garden-update`'s `git pull --ff-only` fails.
- `Dockerfile` (`python:3.11-slim` + fswebcam + tini) and `docker-compose.yml` run `api`, `mqtt`,
  and an optional mosquitto `broker`; containers reach the host's `pigpiod` via
  `PIGPIO_HOST=host.docker.internal`, so no privileged GPIO access is needed.
- `requirements.txt` is the pinned Pi/runtime set (versions chosen for ARM wheels — be cautious
  bumping); `requirements-dev.txt` is pure-Python for CI. Tooling targets Python 3.9+ (`pyproject`,
  CI matrix 3.9/3.11; `logging.basicConfig(force=True)` needs ≥3.8). The README's "3.6+" is
  historical. Verified on hardware 2026-09: a Pi Zero W on Raspberry Pi OS Lite **Trixie** 32-bit
  (Python 3.13) installs and runs cleanly — `pigpio` 1.78 comes from the Raspbian repo (Raspberry
  Pi's own trixie archive dropped it) and every compiled pin has a cp313 wheel on piwheels. On
  that image `sudo` prompts for a password, so `setup.sh` can stall on a prompt after the long
  pip stage.
- `app/integrations/` (Alexa, ThingsBoard) are documented no-op scaffolds behind `*_ENABLED` flags.
- `app/sensors/pcb_temp/over_temp_monitor.py` is a standalone demo script (own `PiGPIOFactory`,
  blocking loop) that nothing imports or installs; don't run it alongside the services.

## Hardware notes that affect code

- Requires the **pigpiod** daemon (hardware-timed PWM: light 8 kHz on GPIO 18, pump 50 Hz on
  GPIO 24). Drivers use the shared `PiGPIOFactory`, never the default gpiozero factory.
- Pins (BCM, from `config.py`): distance echo 19 / trigger 26, button 13, over-temp alert 25.
  The README hardware section has drifted from `config.py` in places; config is what runs.
- I2C addresses: PCT2075 `0x48` (PCB temp), INA219 `0x40` (pump power), DHT20 `0x38`,
  AM2320 `0x5C`. The AM2320 needs a wakeup sequence and won't reliably show in `i2cdetect`,
  which is why `detect_model()` also falls back to `SENSOR_TYPE`.
- Pump `speed` maps 1:1 to PWM duty (0–100 %) and **nothing in code caps it**: `on()` sets 100 %
  despite its "30 percent" docstring (true since the first commit), `DEFAULT_PUMP_SPEED` is 100,
  and the HA pump entity commands 0–100. The README's "max duty 30 %" is an unenforced hardware
  note. The distance driver averages the median of several samples and raises `MeasurementError`.
- Cameras are `/dev/video0` / `/dev/video2` by default; `services/etc/udev/rules.d/` pins them to
  `/dev/gardyn-upper` / `/dev/gardyn-lower` by USB port once edited for the unit.

## Commit & PR conventions

- Conventional Commits (`CONTRIBUTORS.md`): `<type>(<scope>): <description>` with type ∈
  `feat|fix|docs|style|refactor|test|chore`, `!` + `BREAKING CHANGE:` footer for breaking changes.
  `release-please` builds `CHANGELOG.md` and version tags from these on every push to `main` —
  never edit the changelog by hand.
- PR titles are checked by `enforce-pr-title.yml`: must start with one of
  `feat, fix, hotfix, jira, doc, test, proj-, ci` (case-insensitive) and be 5–50 characters.
  Note `chore:`/`refactor:`/`style:` PR titles **fail** this check even though they are valid
  commit types.
