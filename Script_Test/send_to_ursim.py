"""
URSim Auto-Deploy & Run
1. Power ON & brake release
2. Register write (Modbus)
3. Send .script via Secondary Interface
4. Cycle loop with sensor simulation

Standalone module. All URSim-specific logic (setup, test execution,
sensor sim) lives here. Shared utilities are imported from ur_common.py.
"""

import json
import os
import queue
import random
import sys
import threading
import time

from ur_common import (
    dashboard_command, send_script, prepare_script,
    classify_program_files, resolve_path,
    power_on_and_brake_release,
    start_log, stop_log,
)
from ur_report import save_report

try:
    import tkinter as tk
    HAS_TK = True
except ImportError:
    HAS_TK = False

SCENARIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "sensor_scenario.json")

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "ursim_config.json")

def load_config():
    """Load all settings from ursim_config.json (required)."""
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
        ursim = raw["ursim"]
        send = raw["send"]
        paths = raw["paths"]
        sr = raw["sensor_registers"]
        cfg = {
            "ip": ursim["ip"],
            "port": ursim["port"],
            "dashboard_port": ursim["dashboard_port"],
            "auto_wrap": send["auto_wrap"],
            "wrap_func_name": send["wrap_func_name"],
            "connect_timeout": send["connect_timeout"],
            "program_dir": paths["program_dir"],
            "sim_reg_di0": sr["DI0"],
        }
    except KeyError as e:
        print(f"Error: Missing required config key: {e}")
        print(f"  Check {CONFIG_FILE}")
        sys.exit(1)

    return cfg


def load_scenario():
    """Load sensor_scenario.json and return step list and loop setting"""
    if not os.path.exists(SCENARIO_FILE):
        print(f"  Warning: {SCENARIO_FILE} not found")
        return [], True

    try:
        with open(SCENARIO_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        steps = raw.get("sequence", [])
        loop = raw.get("loop", True)
        print(f"  Scenario loaded: {os.path.basename(SCENARIO_FILE)} ({len(steps)} steps, loop={loop})")
        return steps, loop
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  Warning: Failed to load scenario ({e})")
        return [], True


class SensorMonitorWindow:
    """Popup window showing DI0 sensor simulation state"""

    BG = "#1e1e1e"
    FG = "#d4d4d4"
    GREEN = "#4ec9b0"
    GRAY = "#6a6a6a"
    YELLOW = "#dcdcaa"
    ACCENT = "#569cd6"

    def __init__(self, log_file=None):
        self._queue = queue.Queue()
        self._thread = None
        self.root = None
        self._log_file = None
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            self._log_file = open(log_file, "w", encoding="utf-8")

    def start(self):
        if not HAS_TK:
            print("  [SensorMonitor] tkinter not installed, monitor disabled")
            return
        self._thread = threading.Thread(target=self._run)
        self._thread.start()
        time.sleep(0.5)

    # ---- GUI setup (runs in separate thread) ----

    def _run(self):
        self.root = tk.Tk()
        self.root.title("Sensor Simulator")
        self.root.geometry("420x360+50+50")
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(True, True)

        # --- Status display area ---
        state = tk.Frame(self.root, bg=self.BG)
        state.pack(fill=tk.X, padx=10, pady=(10, 5))

        self.di0_frame = tk.Frame(state, bg="#333", relief=tk.RIDGE, bd=1)
        self.di0_frame.pack(fill=tk.X)
        self.di0_lbl = tk.Label(self.di0_frame, text="DI0  --", font=("Consolas", 14, "bold"),
                                fg=self.GRAY, bg="#333", padx=8, pady=6)
        self.di0_lbl.pack()
        tk.Label(self.di0_frame, text="item_detect", font=("Consolas", 8),
                 fg=self.GRAY, bg="#333").pack()

        self.step_lbl = tk.Label(state, text="", font=("Consolas", 10),
                                 fg=self.ACCENT, bg=self.BG, anchor=tk.W)
        self.step_lbl.pack(fill=tk.X, pady=(8, 0))

        # --- Log area ---
        log_frame = tk.LabelFrame(self.root, text=" Log ", font=("Consolas", 9),
                                  fg=self.FG, bg=self.BG, padx=5, pady=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        self.log_text = tk.Text(log_frame, font=("Consolas", 9), bg="#111", fg=self.FG,
                                wrap=tk.WORD, state=tk.DISABLED, height=12)
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("on", foreground=self.GREEN)
        self.log_text.tag_configure("off", foreground=self.GRAY)
        self.log_text.tag_configure("wait", foreground=self.YELLOW)
        self.log_text.tag_configure("info", foreground=self.ACCENT)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()
        self.root.mainloop()
        try:
            self.root.withdraw()
        except (tk.TclError, AttributeError):
            pass
        self.root = None

    def _on_close(self):
        try:
            self.root.quit()
        except tk.TclError:
            pass

    # ---- Queue polling ----

    def _poll(self):
        try:
            for _ in range(50):
                msg = self._queue.get_nowait()
                if msg.get("type") == "close":
                    self.root.quit()
                    return
                self._handle(msg)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.root.after(100, self._poll)
        except tk.TclError:
            pass

    def _handle(self, msg):
        try:
            t = msg["type"]
            if t == "di0":
                v = msg["value"]
                self.di0_lbl.config(text=f"DI0  {'ON' if v else 'OFF'}",
                                    fg=self.GREEN if v else self.GRAY)
            elif t == "step":
                self.step_lbl.config(text=msg["text"])
            elif t == "log":
                self.log_text.config(state=tk.NORMAL)
                ts = time.strftime("%H:%M:%S")
                tag = msg.get("tag", "")
                self.log_text.insert(tk.END, f"{ts}  {msg['text']}\n", tag or ())
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    # ---- Thread-safe API ----

    def set_di0(self, value):
        self._queue.put({"type": "di0", "value": value})

    def set_step(self, text):
        self._queue.put({"type": "step", "text": text})

    def log(self, text, tag=""):
        self._queue.put({"type": "log", "text": text, "tag": tag})
        if self._log_file and not self._log_file.closed:
            try:
                ts = time.strftime("%H:%M:%S")
                self._log_file.write(f"{ts}  {text}\n")
                self._log_file.flush()
            except (ValueError, OSError):
                pass

    def stop(self):
        self._queue.put({"type": "close"})
        if self._thread:
            self._thread.join(timeout=5)
        if self._log_file and not self._log_file.closed:
            self._log_file.close()


def run_sensor_simulator(tc, stop_event, steps, loop, di0_reg,
                         monitor=None, stats=None):
    """Sensor simulator for URSim (runs in background thread)

    Executes sensor_scenario.json sequence steps in order.
    Each step processes: delay / DI0 / wait_register.
    """
    ready_addr = tc.CONTROL_REGS["cycle_ready"]

    def mlog(text, tag=""):
        if monitor:
            monitor.log(text, tag)

    def mstep(text):
        if monitor:
            monitor.set_step(text)

    def sim_write(reg, value, label=""):
        result = tc.write_register(reg, value)
        if result is None:
            mlog(f"WRITE FAIL reg{reg}={value} {label}", "wait")
            time.sleep(0.1)
            result = tc.write_register(reg, value)
            if result is None:
                mlog(f"WRITE FAIL (retry) reg{reg}={value}", "wait")
                return False
        return True

    if not steps:
        mlog("No steps defined, stopping", "info")
        return

    iteration = 0
    print(f"  [SensorSim] Started")
    mlog(f"Simulator started", "info")

    while not stop_event.is_set():
        iteration += 1
        mlog(f"--- Cycle {iteration} ---", "info")

        # First iteration: extra wait for robot to reach home
        if iteration == 1:
            cold_start_delay = 3.0
            mlog(f"Cold start delay ({cold_start_delay}s) ...", "info")
            end_time = time.time() + cold_start_delay
            while time.time() < end_time and not stop_event.is_set():
                time.sleep(min(0.3, end_time - time.time()))

        for i, step in enumerate(steps):
            if stop_event.is_set():
                break

            comment = step.get("comment", "")
            slabel = f"{i+1}/{len(steps)}"

            # delay (number or [min, max] for random)
            raw_delay = step.get("delay", 0)
            if isinstance(raw_delay, list):
                delay = random.uniform(raw_delay[0], raw_delay[1])
            else:
                delay = raw_delay
            if delay > 0:
                mstep(f"Cycle {iteration}  Step {slabel}: delay {delay:.1f}s")
                end_time = time.time() + delay
                while time.time() < end_time and not stop_event.is_set():
                    time.sleep(min(0.3, end_time - time.time()))
                if stop_event.is_set():
                    break

            # wait_register: wait until specified register equals specified value
            wr = step.get("wait_register")
            if wr:
                reg_addr = wr["reg"]
                reg_val = wr["value"]

                mstep(f"Cycle {iteration}  Step {slabel}: wait reg{reg_addr}=={reg_val}")
                mlog(f"wait reg{reg_addr}=={reg_val} ...", "wait")

                while not stop_event.is_set():
                    cur = tc.read_register(reg_addr)
                    if cur is not None and cur == reg_val:
                        break
                    time.sleep(0.3)
                if stop_event.is_set():
                    break

                if stats is not None and reg_addr == ready_addr and reg_val == 0:
                    stats["picks"] += 1
                mlog(f"reg{reg_addr}=={reg_val} OK", "info")

            # DI0 register write + monitor update
            if "DI0" in step:
                v = step["DI0"]
                sim_write(di0_reg, v, f"DI0={v}")
                if monitor:
                    monitor.set_di0(v)
                tag = "on" if v else "off"
                suffix = f"  {comment}" if comment else ""
                mlog(f"DI0={'ON' if v else 'OFF'}{suffix}", tag)

        if not loop:
            break

    print("  [SensorSim] Stopped")
    mlog("Simulator stopped", "info")
    mstep("Stopped")


def _wait_for_sensor_completion(tc, cycle_times, initial_count, sim_stats):
    """Wait for sensor simulator to finish processing all cycles."""
    completed = len(cycle_times)
    expected_cnt = initial_count + completed

    count_addr = tc.CONTROL_REGS["cycle_count"]
    final_count = tc.read_register(count_addr) or 0
    need_wait = (final_count < expected_cnt) or (sim_stats["picks"] < completed)
    if need_wait:
        print("\n  Waiting for verification ...", end="", flush=True)
        for _ in range(30):
            time.sleep(1)
            final_count = tc.read_register(count_addr) or 0
            if final_count >= expected_cnt and sim_stats["picks"] >= completed:
                break
        print(" done")


def run_test(cfg, tc, program_dir, script_files, test,
             sensor_monitor=None):
    """Run STEP 3-5 for URSim test.

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

    # Initialize DI0 sensor register
    di0_reg = cfg["sim_reg_di0"]
    tc.write_register(di0_reg, 0)
    print(f"  Sensor register init (DI0 reg {di0_reg} -> 0)")

    # ---- STEP 4: Send script ----
    print(f"\n[STEP 4] Send script (port {cfg['port']})")
    print("-" * 55)

    for script_name in script_files:
        filepath = os.path.join(program_dir, script_name)
        content, name = prepare_script(cfg, filepath, ursim_transforms=True)
        if not send_script(cfg, content, name):
            return {"verdict": "FAIL", "reason": "script send failed"}

    print("  Waiting for program start (2s) ...")
    time.sleep(2)

    # ---- STEP 5: Cycle loop ----
    print(f"\n[STEP 5] Cycle loop ({len(cycles)} cycles)")
    print("-" * 55)
    tc.print_cycle_table(cycles)

    # Start sensor simulator
    print(f"\n  [SensorSim] Loading scenario")
    sim_steps, sim_loop = load_scenario()

    owns_monitor = sensor_monitor is None
    if owns_monitor:
        sensor_monitor = SensorMonitorWindow()
        sensor_monitor.start()

    sim_stats = {"picks": 0}

    sensor_stop = threading.Event()
    sensor_thread = threading.Thread(
        target=run_sensor_simulator,
        args=(tc, sensor_stop, sim_steps, sim_loop),
        kwargs={"di0_reg": di0_reg, "monitor": sensor_monitor, "stats": sim_stats},
        daemon=True)
    sensor_thread.start()

    initial_count = tc.read_register(tc.CONTROL_REGS["cycle_count"]) or 0

    try:
        cycle_times, cycle_verify, cycle_details, send_error, _ = \
            tc.run_cycle_loop(cycles)

        # Wait for sensor simulator to complete
        _wait_for_sensor_completion(tc, cycle_times, initial_count, sim_stats)

        # Final verification
        result = tc.verify_test_result(
            cycles, cycle_times, cycle_verify,
            initial_count, sim_stats["picks"], send_error)

        if sensor_monitor:
            v = result["verdict"]
            sensor_monitor.log(
                f"=== {v}: detect={result['cycle_count_delta']}"
                f" pick={result['picks']} verify={result['verify_pass']}/{result['completed']}"
                f" / {result['completed']} cycles ===",
                "info" if v == "PASS" else "wait")

        # Add report data
        result["target"] = "URSim"
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
        print("  Check URSim log tab for script errors.")
        res = dashboard_command(cfg, "stop")
        print(f"  Dashboard: {res}")
        return {"verdict": "FAIL", "reason": f"timeout: {e}"}
    except KeyboardInterrupt:
        print("\n\n*** Ctrl+C ***")
        res = dashboard_command(cfg, "stop")
        print(f"  Dashboard: {res}")
        return {"verdict": "FAIL", "reason": "interrupted"}
    finally:
        sensor_stop.set()
        sensor_thread.join(timeout=5)
        if owns_monitor:
            sensor_monitor.stop()


def setup(cfg):
    """STEP 1-2: Robot startup."""
    print(f"\n[STEP 1] File transfer -> skip (script sent via port {cfg['port']})")
    print(f"\n[STEP 2] Robot startup (Dashboard :{cfg['dashboard_port']})")
    print("-" * 55)
    power_on_and_brake_release(cfg)


# =========================================================================
#  Main
# =========================================================================
def main():
    from datetime import datetime
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")
    log_path = start_log(report_dir, session_ts, "URSim")

    cfg = load_config()
    program_dir = resolve_path(cfg, "program_dir")

    print("=" * 55)
    print("  URSim Auto-Deploy & Run")
    print("=" * 55)

    # --- Classify files ---
    all_files, script_files = classify_program_files(program_dir)

    print(f"\nProgram dir: {program_dir}")
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

    # STEP 1-2: Setup
    setup(cfg)

    # STEP 3-5: Register write & execution
    test_controller_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_controller.py")
    has_test_controller = os.path.exists(test_controller_path)

    if not has_test_controller:
        print("\n[STEP 3] test_controller.py not found -> skip")
    elif not script_files:
        print("\n[STEP 3] No .script file -> skip")

    if not script_files:
        print("\n[STEP 4] No .script file -> skip")
        print("\nDone.")
        return

    if not has_test_controller:
        print(f"\n[STEP 4] Send script (port {cfg['port']})")
        print("-" * 55)
        for sname in script_files:
            filepath = os.path.join(program_dir, sname)
            content, name = prepare_script(cfg, filepath, ursim_transforms=True)
            if not send_script(cfg, content, name):
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

    # Run STEP 3-5
    sensor_log_path = os.path.join(report_dir, f"SensorLog_URSim_{session_ts}.log")
    sensor_monitor = SensorMonitorWindow(log_file=sensor_log_path)
    sensor_monitor.start()
    try:
        result = run_test(cfg, tc, program_dir, script_files, test,
                          sensor_monitor=sensor_monitor)
        save_report(result, report_dir=report_dir,
                    session_ts=session_ts, log_file=log_path,
                    sensor_log_file=sensor_log_path)
    finally:
        stop_log()
        sensor_monitor.stop()


if __name__ == "__main__":
    main()
