"""
Shared utilities for UR robot deployment scripts.

Used by send_to_ursim.py and send_to_robot.py.

Shared utilities only. Do NOT add URSim-specific or real-robot-specific
logic here. Module-specific logic belongs in send_to_ursim.py or
send_to_robot.py.
"""

import os
import re
import socket
import sys
import time


# =========================================================================
#  Log Tee (stdout/stderr -> console + file)
# =========================================================================

class LogTee:
    """Duplicate stdout/stderr writes to a log file."""

    def __init__(self, filepath):
        self._filepath = filepath
        self._file = None
        self._orig_stdout = None
        self._orig_stderr = None

    def start(self):
        os.makedirs(os.path.dirname(self._filepath), exist_ok=True)
        self._file = open(self._filepath, "w", encoding="utf-8")
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = self._TeeStream(self._orig_stdout, self._file)
        sys.stderr = self._TeeStream(self._orig_stderr, self._file)

    def stop(self):
        if self._orig_stdout is not None:
            sys.stdout = self._orig_stdout
        if self._orig_stderr is not None:
            sys.stderr = self._orig_stderr
        if self._file and not self._file.closed:
            self._file.close()

    @property
    def filepath(self):
        return self._filepath

    class _TeeStream:
        def __init__(self, original, logfile):
            self._original = original
            self._logfile = logfile

        @property
        def encoding(self):
            return self._original.encoding

        def write(self, text):
            self._original.write(text)
            try:
                self._logfile.write(text)
            except (ValueError, OSError):
                pass

        def flush(self):
            self._original.flush()
            try:
                self._logfile.flush()
            except (ValueError, OSError):
                pass

        def __getattr__(self, name):
            return getattr(self._original, name)


_active_log_tee = None


def start_log(report_dir, session_ts, target=""):
    """Start logging stdout/stderr to report_dir/Log_{target}_{session_ts}.log.

    Returns the log file path.
    """
    global _active_log_tee
    if _active_log_tee is not None:
        return _active_log_tee.filepath
    os.makedirs(report_dir, exist_ok=True)
    prefix = f"Log_{target}_" if target else "Log_"
    log_path = os.path.join(report_dir, f"{prefix}{session_ts}.log")
    _active_log_tee = LogTee(log_path)
    _active_log_tee.start()
    return log_path


def stop_log():
    """Stop logging and close the log file."""
    global _active_log_tee
    if _active_log_tee is not None:
        _active_log_tee.stop()
        _active_log_tee = None


# =========================================================================
#  Path Resolution
# =========================================================================

def resolve_path(cfg, key):
    """Resolve a path from config, relative to this script's directory."""
    path = cfg[key]
    if path == ".":
        return os.path.dirname(os.path.abspath(__file__))
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


# =========================================================================
#  Dashboard Server Communication
# =========================================================================

def dashboard_command(cfg, command, timeout=5):
    """Send command to UR Dashboard Server and return response."""
    try:
        with socket.create_connection((cfg["ip"], cfg["dashboard_port"]), timeout=timeout) as s:
            s.recv(4096)
            s.sendall((command + "\n").encode("utf-8"))
            return s.recv(4096).decode("utf-8").strip()
    except Exception as e:
        return f"Error: {e}"


# =========================================================================
#  Setup Helpers (shared by send_to_ursim / send_to_robot)
# =========================================================================

def power_on_and_brake_release(cfg):
    """Power ON, brake release, verify RUNNING state via Dashboard.

    Calls sys.exit(1) if robot does not reach RUNNING state.
    """
    # Clear any leftover popups / protective stops from previous run
    for cmd in ["close popup", "close safety popup", "unlock protective stop"]:
        res = dashboard_command(cfg, cmd)
        print(f"  {cmd} ... {res}")

    # Check current state — skip power-on sequence if already RUNNING
    mode = dashboard_command(cfg, "robotmode") or ""
    print(f"  robotmode: {mode}")

    if "RUNNING" in mode.upper():
        print("  Already RUNNING, skipping power on / brake release")
    else:
        print("  power on ... ", end="", flush=True)
        res = dashboard_command(cfg, "power on")
        print(res)

        print("  Waiting for power ON ... ", end="", flush=True)
        for _ in range(30):
            time.sleep(1)
            mode = dashboard_command(cfg, "robotmode")
            if mode and ("IDLE" in mode.upper() or "RUNNING" in mode.upper()):
                break
        print(mode)

        print("  brake release ... ", end="", flush=True)
        res = dashboard_command(cfg, "brake release")
        print(res)

        print("  Waiting for brake release ... ", end="", flush=True)
        for _ in range(30):
            time.sleep(1)
            mode = dashboard_command(cfg, "robotmode")
            if mode and "RUNNING" in mode.upper():
                break
        print(mode)

    pre_mode = dashboard_command(cfg, "robotmode")
    pre_safety = dashboard_command(cfg, "safetystatus")
    print(f"\n  [Check] robotmode: {pre_mode}")
    print(f"  [Check] safetystatus: {pre_safety}")
    if "RUNNING" not in (pre_mode or "").upper():
        print("  Robot is not in RUNNING state. Aborting.")
        sys.exit(1)

    # Stop any running program (prevent register conflicts)
    print("\n  Stopping running program ... ", end="", flush=True)
    res = dashboard_command(cfg, "stop")
    print(res)
    time.sleep(1)


# =========================================================================
#  Script Send (Secondary Interface)
# =========================================================================

def send_script(cfg, content, script_name):
    """Send URScript content via Secondary Interface."""
    ip, port = cfg["ip"], cfg["port"]
    print(f"  Target: {ip}:{port}")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(cfg["connect_timeout"])
            s.connect((ip, port))

            data = content.encode("utf-8")
            s.sendall(data)
            print(f"  Sent ({len(data)} bytes)")

            print(f"  -> {script_name} sent successfully")
            return True

    except ConnectionRefusedError:
        print(f"  Error: Connection refused ({ip}:{port})")
        return False
    except socket.timeout:
        print(f"  Error: Connection timeout")
        return False
    except Exception as e:
        print(f"  Error: {e}")
        return False


# =========================================================================
#  Script Manipulation Helpers
# =========================================================================

def get_existing_func_name(script_content):
    """Return function name if script is already wrapped in def/end"""
    stripped = script_content.strip()
    lines = stripped.splitlines()
    if not lines:
        return None
    first = lines[0].strip()
    last = lines[-1].strip()
    if first.startswith("def ") and last == "end":
        name = first[4:].split("(")[0].strip()
        return name
    return None


def ensure_function_call(script_content):
    """Append function call at end of script if missing"""
    func_name = get_existing_func_name(script_content)
    if func_name:
        lines = script_content.strip().splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped == f"{func_name}()" or stripped.startswith(f"{func_name}("):
                if not line.startswith(" ") and not line.startswith("\t") and not line.startswith("def "):
                    return script_content
        return script_content.rstrip() + f"\n{func_name}()\n"
    return script_content


def needs_wrapper(script_content):
    """Check if script needs def/end wrapping"""
    stripped = script_content.strip()
    lines = stripped.splitlines()
    if not lines:
        return True
    return not (lines[0].strip().startswith("def ") and lines[-1].strip() == "end")


def wrap_script(script_content, func_name):
    """Wrap bare script in def/end function block"""
    wrapped = f"def {func_name}():\n"
    for line in script_content.splitlines():
        wrapped += f"  {line}\n"
    wrapped += "end\n"
    wrapped += f"{func_name}()\n"
    return wrapped


# =========================================================================
#  Script Preparation
# =========================================================================

def prepare_script(cfg, filepath, ursim_transforms=False):
    """Read script file and prepare it for sending.

    ursim_transforms: if True, replace DI0 sensor reads with register reads (URSim).
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    script_name = os.path.basename(filepath)
    print(f"  File: {script_name} ({len(content.splitlines())} lines)")

    if ursim_transforms:
        content, sensor_replaced = replace_sensor_with_registers(
            content, cfg["sim_reg_di0"])
        if sensor_replaced:
            print(f"  Sensor -> register replacement: {sensor_replaced} occurrences")

    if cfg["auto_wrap"]:
        if needs_wrapper(content):
            content = wrap_script(content, cfg["wrap_func_name"])
            print(f"  Wrapping: auto-wrapped in def/end")
        else:
            original = content
            content = ensure_function_call(content)
            if content != original:
                func_name = get_existing_func_name(original)
                print(f"  Added function call: {func_name}()")

    return content, script_name


# =========================================================================
#  URSim-only Transformation (DI0 sensor -> register read)
# =========================================================================

def replace_sensor_with_registers(script_content, di0_reg):
    """Replace DI0 sensor reads with register reads (for URSim)."""
    script_content, count = re.subn(
        r"get_standard_digital_in\(0\)",
        f"(read_port_register({di0_reg}) != 0)",
        script_content)
    return script_content, count


# =========================================================================
#  File Classification Helper
# =========================================================================

def classify_program_files(program_dir):
    """Classify files in program directory.

    Returns (all_files, script_files).
    Exits with error if directory is missing or empty.
    """
    if not os.path.isdir(program_dir):
        print(f"Error: {program_dir} not found")
        sys.exit(1)

    all_files = sorted(f for f in os.listdir(program_dir)
                       if os.path.isfile(os.path.join(program_dir, f)))
    if not all_files:
        print(f"Error: {program_dir} is empty")
        sys.exit(1)

    script_files = [f for f in all_files if f.endswith(".script")]

    return all_files, script_files
