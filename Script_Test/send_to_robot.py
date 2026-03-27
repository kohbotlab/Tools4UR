"""
Real Robot Auto-Deploy & Run

Method A (Secondary Interface - development/PoC):
  1. Skip (files already on robot via teach pendant)
  2. Power ON & brake release
  3. Register write (Modbus)
  4. Send .script via Secondary Interface
  5. Cycle loop

Method B (SCP + Dashboard play - production):
  1. SCP .script to robot
  2. Power ON & brake release
  3. Register write (Modbus)
  4. Dashboard load call.urp + play
  5. Cycle loop

Standalone module. All real-robot-specific logic (setup, test execution,
SCP transfer, dashboard play) lives here. Shared utilities are imported
from ur_common.py.
"""

import json
import os
import sys
import time

from ur_common import (
    dashboard_command, send_script, prepare_script,
    classify_program_files, resolve_path,
    power_on_and_brake_release,
    start_log, stop_log,
)
from ur_report import save_report

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "robot_config.json")

def load_config():
    """Load all settings from robot_config.json (required)."""
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Config file not found: {CONFIG_FILE}")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {CONFIG_FILE}: {e}")
        sys.exit(1)

    try:
        robot = raw["robot"]
        send = raw["send"]
        paths = raw["paths"]
        ssh = raw["ssh"]
        cfg = {
            "ip": robot["ip"],
            "port": robot["port"],
            "dashboard_port": robot["dashboard_port"],
            "auto_wrap": send["auto_wrap"],
            "wrap_func_name": send["wrap_func_name"],
            "connect_timeout": send["connect_timeout"],
            "program_dir": paths["program_dir"],
            "robot_program_dir": paths["robot_program_dir"],
            "ssh_user": ssh["user"],
            "ssh_password": ssh["password"],
            "ssh_port": ssh["port"],
            "ssh_robot_program_dir": ssh["robot_program_dir"],
            "urp_program": ssh["urp_program"],
        }
    except KeyError as e:
        print(f"Error: Missing required config key: {e}")
        print(f"  Check {CONFIG_FILE}")
        sys.exit(1)

    return cfg


def scp_files_to_robot(cfg, program_dir, file_list):
    """Transfer files to robot via SSH/SFTP (paramiko)"""
    ip = cfg["ip"]
    user = cfg["ssh_user"]
    password = cfg["ssh_password"]
    ssh_port = cfg["ssh_port"]
    dest_dir = cfg["ssh_robot_program_dir"]

    print(f"  SSH target: {user}@{ip}:{ssh_port}")
    print(f"  Destination: {dest_dir}/")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(ip, port=ssh_port, username=user, password=password, timeout=10)
        sftp = ssh.open_sftp()

        failed = []
        for filename in file_list:
            local_path = os.path.join(program_dir, filename)
            remote_path = dest_dir.rstrip("/") + "/" + filename
            print(f"  {filename} ... ", end="", flush=True)
            try:
                sftp.put(local_path, remote_path)
                print("OK")
            except Exception as e:
                print(f"FAILED ({e})")
                failed.append(filename)

        sftp.close()
        ssh.close()

        if failed:
            print(f"\n  Error: {len(failed)} file(s) failed to transfer.")
            return False
        print(f"  -> {len(file_list)} file(s) transferred")
        return True

    except Exception as e:
        print(f"  SSH connection error: {e}")
        return False


def dashboard_load_and_play(cfg):
    """Load .urp and start execution via Dashboard play"""
    urp = cfg["urp_program"]
    robot_dir = cfg["robot_program_dir"]
    urp_path = robot_dir + "/" + urp

    print(f"  load {urp_path} ... ", end="", flush=True)
    res = dashboard_command(cfg, f"load {urp_path}", timeout=15)
    print(res)

    if "error" in (res or "").lower():
        print(f"  Error: Failed to load program")
        return False

    time.sleep(1)

    print(f"  play ... ", end="", flush=True)
    res = dashboard_command(cfg, "play")
    print(res)

    if "error" in (res or "").lower():
        print(f"  Error: Failed to play program")
        return False

    return True


def run_test(cfg, tc, program_dir, script_files, test, method="A"):
    """Run STEP 3-5 for real robot test.

    Returns result dict with verdict, stats, etc.
    """
    cycles = test["cycles"]

    # ---- STEP 3: Register write ----
    print(f"\n[STEP 3] Register write: {test['desc']}")
    print("-" * 55)

    # Initialize control registers to known state
    print("\n  [Control Registers - reset]")
    for name, addr in tc.CONTROL_REGS.items():
        result = tc.write_register(addr, 0)
        status = "OK" if result is not None else "FAIL"
        print(f"  Reg {addr:3d} ({name:15s}) = 0 ... {status}")

    print("\n  [Fixed Registers]")
    ok, ng = tc.write_and_verify(tc.FIXED_REGISTERS)
    if ng:
        print(f"  ERROR: {ng} fixed register(s) failed.")
        return {"verdict": "FAIL", "reason": "register write failed"}

    print("\n  [Dynamic Registers - initial]")
    ok, ng = tc.write_and_verify(tc.DYNAMIC_REGISTERS)
    if ng:
        print(f"  ERROR: {ng} dynamic register(s) failed.")
        return {"verdict": "FAIL", "reason": "register write failed"}

    print("\n  Register write complete")

    # ---- STEP 4: Execute program ----
    if method == "A":
        print(f"\n[STEP 4] Send script (Method A: port {cfg['port']})")
        print("-" * 55)
        for script_name in script_files:
            filepath = os.path.join(program_dir, script_name)
            content, name = prepare_script(cfg, filepath, ursim_transforms=False)
            if not send_script(cfg, content, name):
                return {"verdict": "FAIL", "reason": "script send failed"}
    else:
        print(f"\n[STEP 4] Dashboard load & play (Method B: {cfg['urp_program']})")
        print("-" * 55)
        if not dashboard_load_and_play(cfg):
            return {"verdict": "FAIL", "reason": "dashboard load/play failed"}

    print("  Waiting for program start (2s) ...")
    time.sleep(2)

    # ---- STEP 5: Cycle loop ----
    print(f"\n[STEP 5] Cycle loop ({len(cycles)} cycles)")
    print("-" * 55)
    tc.print_cycle_table(cycles)

    initial_count = tc.read_register(tc.CONTROL_REGS["cycle_count"]) or 0

    try:
        cycle_times, cycle_verify, cycle_details, send_error, picks = \
            tc.run_cycle_loop(cycles, count_picks=True)

        # Final verification
        result = tc.verify_test_result(
            cycles, cycle_times, cycle_verify,
            initial_count, picks, send_error)

        # Add report data
        result["target"] = "Robot"
        result["description"] = test.get("desc", "")
        result["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        result["script_files"] = list(script_files)
        result["fixed_registers"] = dict(tc.FIXED_REGISTERS)
        result["cycle_details"] = cycle_details

        # Stop robot program
        print("\n  Stopping robot program ... ", end="", flush=True)
        res = dashboard_command(cfg, "stop")
        print(res)

        return result

    except TimeoutError as e:
        print(f"\n\n  TIMEOUT: {e}")
        res = dashboard_command(cfg, "stop")
        print(f"  Dashboard: {res}")
        return {"verdict": "FAIL", "reason": f"timeout: {e}"}
    except KeyboardInterrupt:
        print("\n\n*** Ctrl+C ***")
        res = dashboard_command(cfg, "stop")
        print(f"  Dashboard: {res}")
        return {"verdict": "FAIL", "reason": "interrupted"}


def setup(cfg, program_dir, all_files, method="A"):
    """STEP 1-2: File transfer, robot startup.

    Returns True on success, calls sys.exit(1) on fatal errors.
    """
    # =========================================================
    # STEP 1: File transfer
    # =========================================================
    if method == "A":
        print(f"\n[STEP 1] File transfer -> skip (Method A: files already on robot)")
    else:
        transfer_files = [f for f in all_files if f.endswith(".script")]
        print(f"\n[STEP 1] SCP file transfer ({len(transfer_files)} files)")
        print("-" * 55)
        if not transfer_files:
            print("  No .script files to transfer")
        else:
            if not scp_files_to_robot(cfg, program_dir, transfer_files):
                sys.exit(1)

    # =========================================================
    # STEP 2: Power ON & brake release
    # =========================================================
    print(f"\n[STEP 2] Robot startup (Dashboard :{cfg['dashboard_port']})")
    print("-" * 55)

    # Remote Control check (real robot only)
    rc = dashboard_command(cfg, "is in remote control")
    if "true" not in (rc or "").lower():
        print(f"  WARNING: Robot is not in Remote Control mode ({rc})")
        print("  -> Switch to Remote Control on teach pendant, then press Enter")
        input()

    power_on_and_brake_release(cfg)
    return True


# =========================================================================
#  Main
# =========================================================================
def main():
    from datetime import datetime
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")
    log_path = start_log(report_dir, session_ts, "Robot")

    try:
        cfg = load_config()
        program_dir = resolve_path(cfg, "program_dir")

        print("=" * 55)
        print("  Real Robot Auto-Deploy & Run")
        print("=" * 55)

        # --- Classify files ---
        all_files, script_files = classify_program_files(program_dir)

        print(f"\nProgram dir: {os.path.normpath(program_dir)}")
        print(f"Files:")
        for f in all_files:
            print(f"  - {f}")

        # --- Script selection ---
        if len(script_files) > 1:
            print("\n  [Script Files]")
            for i, sname in enumerate(script_files, 1):
                print(f"    {i}: {sname}")
            try:
                idx = int(input("\n  Select script number: ")) - 1
                if 0 <= idx < len(script_files):
                    script_files = [script_files[idx]]
                else:
                    print("  Invalid selection. Aborting.")
                    return
            except (ValueError, EOFError):
                print("  Invalid input. Aborting.")
                return
            print(f"  -> {script_files[0]}")
        elif script_files:
            print(f"\n  Script: {script_files[0]}")

        # --- Method selection ---
        print(f"\n  [A] port {cfg['port']} (development/PoC)")
        print(f"  [B] SCP + Dashboard play (production)")

        if not HAS_PARAMIKO:
            print(f"\n  Note: paramiko not installed -> Method B unavailable")
            print(f"  Install: pip install paramiko")
            method = "A"
            print(f"\n  -> Method A (auto)")
        elif not cfg["urp_program"]:
            print(f"\n  Note: ssh.urp_program not configured -> Method B unavailable")
            method = "A"
            print(f"\n  -> Method A (auto)")
        else:
            try:
                choice = input("\n  Select method [A/B] (default=A): ").strip().upper()
            except EOFError:
                choice = "A"
            method = "B" if choice == "B" else "A"
            print(f"\n  -> Method {method} selected")

        # STEP 1-2: Setup
        setup(cfg, program_dir, all_files, method)

        # STEP 3-5: Register write & execution
        test_controller_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_controller.py")
        has_test_controller = os.path.exists(test_controller_path)

        if not has_test_controller:
            print("\n[STEP 3] test_controller.py not found -> skip")
        elif not script_files:
            print("\n[STEP 3] No .script file -> skip")

        if not script_files:
            print("\n[STEP 4] No .script file -> skip")
            print("\nDone (no script to send)")
            return

        if not has_test_controller:
            # No test controller: just send script and exit
            if method == "A":
                print(f"\n[STEP 4] Send script (Method A: port {cfg['port']})")
                print("-" * 55)
                for sname in script_files:
                    filepath = os.path.join(program_dir, sname)
                    content, name = prepare_script(cfg, filepath, ursim_transforms=False)
                    if not send_script(cfg, content, name):
                        sys.exit(1)
            else:
                print(f"\n[STEP 4] Dashboard load & play (Method B: {cfg['urp_program']})")
                print("-" * 55)
                if not dashboard_load_and_play(cfg):
                    sys.exit(1)
            print("\n" + "=" * 55)
            print("  All steps complete (no test controller)")
            print("=" * 55)
            return

        # Import test controller
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import test_controller as tc
        tc.ROBOT_IP = cfg["ip"]
        print(f"\n  Modbus target: {tc.ROBOT_IP}:{tc.MODBUS_PORT}")

        test = tc.TEST_CASE
        print(f"\n  >>> {test['desc']} ({len(test['cycles'])} cycles)")
        print("  " + "=" * 50)

        # Run STEP 3-5 via run_test()
        result = run_test(cfg, tc, program_dir, script_files, test, method)
        save_report(result, report_dir=report_dir,
                    session_ts=session_ts, log_file=log_path)
    finally:
        stop_log()


if __name__ == "__main__":
    main()
