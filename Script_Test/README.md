# UR Robot Test Automation

Automated test framework for UR robot palletizing.
Supports both URSim (Docker simulator) and real robot.
Current script: `simple_pallet_v1.script` (DI0 sensor, no threads).

## Setup

### Configuration (required before first run)

Copy the example config and edit it to match your environment:

```bash
cp config/robot_config.example.json config/robot_config.json
```

Edit `robot_config.json` and set:
- `robot.ip` — your real robot's IP address
- `ssh.password` — your robot's SSH password

> **Note:** `robot_config.json` and `report/` are excluded from version
> control because they contain credentials and environment-specific values.
> Only the `*.example.json` file is tracked.

## Requirements

- Python 3.10+
- URSim (Docker) or UR real robot
- Windows (for URSim Docker)

```
pip install openpyxl        # Excel report generation
pip install paramiko         # Only for real robot Method B (SCP transfer)
```

## File Structure

```
Gen_URScript/
├── Script_Test/
│   ├── send_to_ursim.py         # URSim standalone (with DI0 sensor simulation)
│   ├── send_to_robot.py         # Real robot standalone (Method A/B)
│   ├── test_controller.py       # Test cases & Modbus register communication
│   ├── ur_common.py             # Shared UR communication utilities
│   ├── ur_report.py             # Excel report generator
│   ├── README.md
│   ├── requirements.txt
│   ├── report/                  # Test reports & logs (auto-generated)
│   └── config/
│       ├── ursim_config.json       # URSim connection settings
│       ├── robot_config.json       # Real robot connection settings
│       ├── test_cases.json         # Register definitions, layouts, test cases
│       └── sensor_scenario.json    # DI0 sensor simulation sequence
└── program/
    ├── simple_pallet_v1.script  # Palletizing URScript
    └── NOTICE.txt               # Safety notice / disclaimer
```

## Usage

```bash
cd Script_Test
python send_to_ursim.py    # URSim (with sensor simulation)
python send_to_robot.py    # Real robot (Method A or B)
```

Both scripts support multiple `.script` files in `program/` — select at runtime.

## Configuration

### URSim (`Script_Test/config/ursim_config.json`)

| Key | Description |
|-----|-------------|
| `ursim.ip` | URSim IP address |
| `ursim.port` | Script send port (Secondary Interface) |
| `ursim.dashboard_port` | Dashboard Server port |
| `paths.program_dir` | URScript file location |
| `sensor_registers.DI0` | DI0 simulation register address |

### Real Robot (`Script_Test/config/robot_config.json`)

| Key | Description |
|-----|-------------|
| `robot.ip` | Robot IP address |
| `robot.port` | Script send port (Secondary Interface) |
| `robot.dashboard_port` | Dashboard Server port |
| `ssh.user` / `ssh.password` | SSH credentials (Method B) |
| `ssh.robot_program_dir` | Program path on robot |
| `ssh.urp_program` | .urp file for Method B |

All values are defined in the respective JSON files. No defaults in code.

## Test Case

Defined in `config/test_cases.json`: single pallet, standard layout (L1).
Runs 10 cycles. Layout parameters (origin, spacing, layer step)
and register addresses are all configurable in `test_cases.json`.

## Register Protocol

Python writes parameters to Modbus registers (port 502), URScript reads them:

- **Fixed registers** (128-131): pallet/item dimensions, Python-only (UR does not read)
- **Dynamic registers** (132-135): offset, target pallet, layer height, written per cycle by Python, read by UR
- **Control registers** (136-137): UR writes during cycle, Python reads for handshake
- **Sensor register** (138): DI0 simulation (URSim only)

Handshake per cycle:
```
Python: write dynamic regs -> wait reg136==1 -> verify -> reset -> wait reg136==0
UR:     wait reg134!=0 -> read regs -> reg136=1 -> pick/place -> reg136=0
```

## Communication Interfaces

| Port | Name | Usage |
|------|------|-------|
| 30002 | Secondary Interface | Send URScript text, interpreter executes immediately |
| 29999 | Dashboard Server | Power on, brake release, stop, load/play .urp |
| 502 | Modbus TCP | Read/write test parameters via holding registers |

## Execution Methods

**Method A** — Send via port 30002 (development/PoC)
```
PC -> read .script -> auto_wrap -> send to 30002 -> execute
```
- External PC required, no files needed on robot filesystem

**Method B** — File deploy + Dashboard play (production, real robot only)
```
PC -> SCP .script to robot -> .urp references .script -> Dashboard play
```
- Requires `call.urp` on robot and `paramiko`
- No port 30002 needed, no auto_wrap needed

### Method B Setup

1. Create `call.urp` on teach pendant (Script node referencing .script)
2. `pip install paramiko`
3. Configure SSH in `config/robot_config.json`
4. If paramiko not installed, Method A is auto-selected

### Comparison

|                            | URSim       | Robot (A)   | Robot (B)   |
|----------------------------|-------------|-------------|-------------|
| File deployment            | skip        | skip        | SCP .script |
| .script execution          | port 30002  | port 30002  | .urp play   |
| Sensor -> register convert | Yes         | No          | No          |
| auto_wrap                  | Yes         | Yes         | No          |
| DI0 sensor simulator       | Yes         | No          | No          |
| Remote Control check       | No          | Yes         | Yes         |

## Execution Flow

### URSim (`send_to_ursim.py`)

```
STEP 1: Skip (script sent via port 30002)
STEP 2: Power ON & brake release
STEP 3: Modbus register write
STEP 4: Read .script -> auto_wrap -> send via port 30002
STEP 5: Cycle loop + DI0 sensor simulator (background thread)
```

Sensor simulation:
- DI0 (item detect) simulated via register 138
- Scenario defined in `config/sensor_scenario.json`
- `SensorMonitorWindow` (tkinter) shows DI0 state and log

### Real Robot (`send_to_robot.py`)

Supports Method A/B selection at runtime.

- **Method A**: STEP 1 skip -> STEP 2 Remote Control check + power on -> STEP 3-5 same as URSim (no sensor sim)
- **Method B**: STEP 1 SCP transfer -> STEP 4 Dashboard load + play -> rest same

## auto_wrap

Port 30002 executes top-level code sequentially.
If a script only has `def/end`, the function is defined but never called.

`auto_wrap` handles this automatically:
1. If no existing `def/end` wrapper, wrap entire script with `wrap_func_name`
2. Append function call at the end (e.g. `main_run()`)

Not needed when executing via PolyScope (`.urp`).

## Module Reference

**ur_common.py** — generic UR communication
- `dashboard_command()` — Dashboard Server command
- `send_script()` — send URScript via port 30002
- `prepare_script()` — read + process script (ursim_transforms flag)
- `classify_program_files()` — list & classify files in program dir
- `power_on_and_brake_release()` — power ON + brake release sequence
- `replace_sensor_with_registers()` — DI0 -> register read (URSim)
- `wrap_script` / `needs_wrapper` / `ensure_function_call` — auto_wrap helpers
- `start_log` / `stop_log` — console + file logging

**test_controller.py** — test logic (loaded from `config/test_cases.json`)
- `TEST_CASE` / `FIXED_REGISTERS` / `DYNAMIC_REGISTERS` — config data
- `read_register` / `write_register` / `write_and_verify` — Modbus I/O
- `update_dynamic` / `reset_dynamic_registers` / `wait_for_register`
- `run_cycle_loop()` — execute cycle loop with verification
- `verify_test_result()` — final result summary

**send_to_ursim.py** — standalone: `python send_to_ursim.py`
- `run_test()` — STEP 3-5
- `run_sensor_simulator()` — DI0 simulation thread
- `SensorMonitorWindow` — tkinter GUI for DI0 state

**send_to_robot.py** — standalone: `python send_to_robot.py`
- `run_test()` — STEP 3-5
- `scp_files_to_robot()` — SSH/SFTP transfer (Method B)
- `dashboard_load_and_play()` — Dashboard load + play (Method B)

**ur_report.py** — report generation
- `save_report()` — generate Excel from `run_test()` result

## Key Rules

- `ur_common.py` = UR generic only. No application logic.
- `test_controller.py` = test logic. Data from `config/test_cases.json`.
- Reg 136/137 are UR-controlled during cycles. Python resets at init only.
- Fixed registers (128-131) are Python-side only. UR does not read them.
- Sensor register 138 is simulation-only (URSim, Python-controlled).
