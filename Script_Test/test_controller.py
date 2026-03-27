import json
import os
import socket
import struct
import time


# =============================================================================
#  UR20 Configuration
# =============================================================================
_ROBOT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "config", "robot_config.json")

def _load_robot_ip():
    """Load robot IP from config/robot_config.json."""
    try:
        with open(_ROBOT_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Error: Config not found: {_ROBOT_CONFIG_PATH}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Error: Invalid JSON in {_ROBOT_CONFIG_PATH}: {e}")
    ip = raw.get("robot", {}).get("ip")
    if not ip:
        raise SystemExit(f"Error: 'robot.ip' not found in {_ROBOT_CONFIG_PATH}")
    return ip

ROBOT_IP       = _load_robot_ip()


# =============================================================================
#  Cycle Generator
# =============================================================================
def _make_cycle(x, y, h, pallet):
    return {
        "offset_x": x,           "offset_y": y,
        "target_pallet":  pallet,
        "layer_height": h,
    }


def make_single_pallet_cycles(cfg, pallet=1, total=10):
    """Generate 2x2 grid cycles for one pallet."""
    x1 = cfg["origin_x"]
    x2 = x1 + cfg["item_x"] + cfg["spacing_x"]
    y1 = cfg["origin_y"]
    y2 = y1 + cfg["item_y"] + cfg["spacing_y"]

    positions = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    cycles = []
    layer  = 0
    while len(cycles) < total:
        h = layer * cfg["layer_step"]
        for x, y in positions:
            if len(cycles) >= total:
                break
            cycles.append(_make_cycle(x, y, h, pallet))
        layer += 1
    return cycles


# =============================================================================
#  Load test case definitions from JSON
# =============================================================================
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "config", "test_cases.json")

_GENERATORS = {
    "single_pallet": make_single_pallet_cycles,
}


def _load_test_config():
    """Load register definitions, layout, and test case from config/test_cases.json."""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # --- Fixed registers ---
    fixed = {}
    for addr_s, entry in raw["fixed_registers"].items():
        fixed[int(addr_s)] = (entry["name"], entry["value"])

    # --- Dynamic registers ---
    dynamic = {}
    for addr_s, entry in raw["dynamic_registers"].items():
        dynamic[int(addr_s)] = (entry["name"], entry["default"])

    total_cycles = raw.get("total_cycles", 10)

    # --- Derived item sizes (lookup by name, not address) ---
    fixed_by_name = {name: value for _, (name, value) in fixed.items()}
    item_x = fixed_by_name["item_w"]
    item_y = fixed_by_name["item_d"]

    # --- Resolve layout ---
    layout_raw = raw.get("layouts", {})
    layouts = {}
    for name, cfg in layout_raw.items():
        cfg = {k: v for k, v in cfg.items() if k != "_comment"}
        cfg.setdefault("item_x", item_x)
        cfg.setdefault("item_y", item_y)
        layouts[name] = cfg

    # --- Build TEST_CASE (single) ---
    tc_def = raw["test_case"]
    gen_func = _GENERATORS[tc_def["generator"]]
    layout = layouts[tc_def["layout"]]
    cycles = gen_func(layout, pallet=tc_def.get("pallet", 1),
                      total=total_cycles)
    test_case = {"desc": tc_def["desc"], "cycles": cycles}

    # --- Control registers (read-only, UR-controlled) ---
    control_regs = {}
    for name, entry in raw.get("control_registers", {}).items():
        control_regs[name] = entry["address"]

    # --- Timing parameters ---
    timing = raw.get("timing", {})

    # --- Modbus port ---
    modbus_port = raw["modbus_port"]

    return fixed, dynamic, test_case, control_regs, timing, modbus_port


FIXED_REGISTERS, DYNAMIC_REGISTERS, TEST_CASE, CONTROL_REGS, TIMING, MODBUS_PORT = _load_test_config()
DYNAMIC_INITIAL = {addr: val for addr, (_, val) in DYNAMIC_REGISTERS.items()}


# =============================================================================
#  Modbus TCP Functions
# =============================================================================
def write_register(address, value):
    """Write single holding register (FC 0x06)."""
    try:
        packet = struct.pack(">HHHBBHH",
                             0x0001, 0x0000, 0x0006, 0x00, 0x06,
                             address, value)
        with socket.create_connection((ROBOT_IP, MODBUS_PORT), timeout=2) as s:
            s.sendall(packet)
            res = s.recv(1024)
            return struct.unpack(">H", res[10:12])[0]
    except Exception as e:
        print(f"  Write Error (Reg {address}): {e}")
        return None


def read_register(address):
    """Read single holding register (FC 0x03)."""
    try:
        packet = struct.pack(">HHHBBHH",
                             0x0001, 0x0000, 0x0006, 0x00, 0x03,
                             address, 1)
        with socket.create_connection((ROBOT_IP, MODBUS_PORT), timeout=2) as s:
            s.sendall(packet)
            res = s.recv(1024)
            return struct.unpack(">H", res[9:11])[0]
    except Exception as e:
        print(f"  Read Error (Reg {address}): {e}")
        return None


def write_and_verify(registers, quiet=False, retries=2):
    """Write registers and verify by read-back. Returns (ok, ng) counts."""
    ok, ng = 0, 0
    for addr, (name, value) in sorted(registers.items(), key=lambda x: (x[1][0] == "target_pallet", x[0])):
        if not quiet:
            print(f"  Reg {addr:3d} ({name:15s}) = {value:5d} ... ", end="")
        success = False
        for attempt in range(1 + retries):
            if attempt > 0:
                time.sleep(0.05)
                write_register(addr, value)
            else:
                result = write_register(addr, value)
                if result is None:
                    print(f"  Reg {addr:3d} FAILED to write {value}")
                    ng += 1
                    success = True  # skip further retries
                    break
            readback = read_register(addr)
            if readback == value:
                if not quiet:
                    print("OK")
                ok += 1
                success = True
                break
        if not success:
            print(f"  Reg {addr:3d} MISMATCH: wrote {value}, read {readback}"
                  f" (after {retries} retries)")
            ng += 1
    return ok, ng


# =============================================================================
#  Dynamic Register Helpers
# =============================================================================
def update_dynamic(quiet=False, **kwargs):
    """Update specific dynamic registers by name."""
    name_to_addr = {name: addr for addr, (name, _) in DYNAMIC_REGISTERS.items()}
    to_write = {}
    for name, value in kwargs.items():
        if name not in name_to_addr:
            print(f"  WARNING: '{name}' is not a dynamic register, skipping")
            continue
        addr = name_to_addr[name]
        DYNAMIC_REGISTERS[addr] = (name, value)
        to_write[addr] = (name, value)
    if to_write:
        return write_and_verify(to_write, quiet=quiet)
    return 0, 0


def reset_dynamic_registers(quiet=False):
    """Reset all dynamic registers to their initial values."""
    for addr, (name, _) in DYNAMIC_REGISTERS.items():
        DYNAMIC_REGISTERS[addr] = (name, DYNAMIC_INITIAL[addr])
    return write_and_verify(DYNAMIC_REGISTERS, quiet=quiet)


def wait_for_register(address, value, timeout=120):
    """Poll register until it equals the expected value.

    Args:
        timeout: max wait in seconds (default 120). 0 = no timeout.

    Raises:
        TimeoutError: if timeout exceeded.
    """
    interval = TIMING.get("ready_poll_interval", 0.5)
    start = time.time()
    while True:
        cur = read_register(address)
        if cur == value:
            return
        if timeout and (time.time() - start) > timeout:
            raise TimeoutError(
                f"Reg {address}: expected {value}, last read {cur} "
                f"(timeout {timeout}s)")
        time.sleep(interval)


# =============================================================================
#  Cycle Loop & Verification (used by send_to_ursim / send_to_robot)
# =============================================================================
def print_cycle_table(cycles):
    """Print cycle parameter table."""
    header = f"  {'#':>3s}  {'ofsX':>5s} {'ofsY':>5s} {'tgtP':>5s} {'layH':>5s}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, p in enumerate(cycles):
        print(f"  {i+1:3d}  "
              f"{p['offset_x']:5d} {p['offset_y']:5d} "
              f"{p['target_pallet']:5d} "
              f"{p['layer_height']:5d}")
    print("  " + "=" * 50)


def run_cycle_loop(cycles, count_picks=False):
    """Execute cycle loop: send dynamic params, wait, verify, reset.

    Args:
        cycles: list of cycle parameter dicts
        count_picks: if True, increment picks counter per completed cycle

    Returns:
        tuple: (cycle_times, cycle_verify, cycle_details, send_error, picks)
    """
    cycle_times = []
    cycle_verify = []
    cycle_details = []
    send_error = False
    picks = 0

    wait_for_register(CONTROL_REGS["cycle_ready"], 0)
    cycle_start = time.time()

    for cycle, p in enumerate(cycles):
        tag = f"Cycle {cycle + 1:2d}/{len(cycles)}"

        if cycle > 0:
            elapsed = time.time() - cycle_start
            remaining = TIMING.get("min_cycle_interval", 7) - elapsed
            if remaining > 0:
                print(f"    (waiting {remaining:.1f}s for min interval)", flush=True)
                time.sleep(remaining)
        cycle_start = time.time()

        print(f"    {tag}: "
              f"ofsX={p['offset_x']:4d} ofsY={p['offset_y']:4d} "
              f"tgtP={p['target_pallet']} "
              f"layH={p['layer_height']:3d}"
              f" -> sending ...", end="", flush=True)
        ok, ng = update_dynamic(quiet=True, **p)
        if ng:
            print(f" SEND ERROR ({ng} failed)")
            send_error = True
            break

        print(" sent -> waiting ...", end="", flush=True)
        wait_for_register(CONTROL_REGS["cycle_ready"], 1)
        if count_picks:
            picks += 1
        cycle_elapsed = time.time() - cycle_start
        cycle_times.append(cycle_elapsed)

        # Parameter readback verification
        mismatches = []
        for addr, (name, _) in DYNAMIC_REGISTERS.items():
            expected = p.get(name)
            if expected is None:
                continue
            actual = read_register(addr)
            if actual != expected:
                mismatches.append(f"{name}(r{addr}):{expected}->{actual}")
        cycle_verify.append(len(mismatches) == 0)
        cycle_details.append({
            "cycle_num": cycle + 1,
            "params": dict(p),
            "time": cycle_elapsed,
            "verify": len(mismatches) == 0,
            "mismatches": list(mismatches),
        })

        if mismatches:
            print(f" done ({cycle_elapsed:.1f}s) VERIFY NG [{', '.join(mismatches)}] -> reset ...",
                  end="", flush=True)
        else:
            print(f" done ({cycle_elapsed:.1f}s) verified -> reset ...", end="", flush=True)

        ok, ng = reset_dynamic_registers(quiet=True)
        if ng:
            time.sleep(0.3)
            ok, ng = reset_dynamic_registers(quiet=True)
        if ng:
            print(f" reset ({ng} mismatch, continuing)")
        else:
            print(" OK")

        if cycle < len(cycles) - 1:
            wait_for_register(CONTROL_REGS["cycle_ready"], 0)

    return cycle_times, cycle_verify, cycle_details, send_error, picks


def verify_test_result(cycles, cycle_times, cycle_verify,
                       initial_count, picks, send_error):
    """Final verification and result summary."""
    completed = len(cycle_times)

    final_count = read_register(CONTROL_REGS["cycle_count"]) or 0
    detect_ok = final_count >= initial_count + completed
    pick_ok = picks >= completed
    verify_pass = sum(1 for v in cycle_verify if v)
    verify_fail = sum(1 for v in cycle_verify if not v)
    verify_ok = verify_fail == 0
    avg_time = sum(cycle_times) / completed if completed else 0

    print("\n" + "=" * 55)
    verdict = "PASS" if (detect_ok and pick_ok and verify_ok and not send_error) else "FAIL"
    print(f"  Result: {verdict}")
    print("-" * 55)
    print(f"    Cycles completed : {completed}/{len(cycles)}")
    print(f"    Param verify     : {verify_pass} OK / {verify_fail} NG"
          f" {'OK' if verify_ok else 'NG'}")
    print(f"    cycle_count      : {initial_count} -> {final_count}"
          f" (delta={final_count - initial_count}, expected={completed})"
          f" {'OK' if detect_ok else 'NG'}")
    print(f"    cycle_ready      : {picks}/{completed}"
          f" {'OK' if pick_ok else 'NG'}")
    print(f"    Avg cycle time   : {avg_time:.1f}s")
    if send_error:
        print(f"    Send error       : YES")
    print("=" * 55)

    return {
        "verdict": verdict,
        "completed": completed,
        "total": len(cycles),
        "avg_time": avg_time,
        "verify_pass": verify_pass,
        "verify_fail": verify_fail,
        "cycle_count_initial": initial_count,
        "cycle_count_final": final_count,
        "cycle_count_delta": final_count - initial_count,
        "cycle_count_ok": detect_ok,
        "picks": picks,
        "pick_ok": pick_ok,
        "send_error": send_error,
    }
