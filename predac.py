import os
import time
import json
import subprocess
import lirc

# Configuration
STATE_FILE = "/var/tmp/amplifier_state.json"
CHECK_INTERVAL = 10  # seconds
SHUTDOWN_DELAY = 1800  # 30 minutes

AUDIO_SOURCE_1_FILE = "/proc/asound/card0/pcm0p/sub0/status"
AUDIO_SOURCE_2_CMD = ["atvremote", "-i", "46:58:80:7B:F0:7C", "power_state"]

# IR commands
IR_POWER_TOGGLE = ["predac", "ONOFF"]
IR_SELECT_SOURCE_1 = ["predac", "OPT2"]
IR_SELECT_SOURCE_2 = ["predac", "OPT1"]

def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{timestamp}] {message}")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            log(f"Loaded state from file: {state}")
            return state
    return {"amp_on": False, "last_active": 0, "current_source": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def is_audio_source_1_on():
    try:
        with open(AUDIO_SOURCE_1_FILE, "r") as f:
            return f.read(14) == "state: RUNNING"
    except Exception:
        return False

def is_audio_source_2_on():
    try:
        result = subprocess.run(AUDIO_SOURCE_2_CMD, capture_output=True, text=True)
        return "PowerState.On" in result.stdout
    except Exception:
        return False

def send_ir_command(lircclient, command):
    try:
       lircclient.send_once(command)
    except Exception as e:
        log(f"Failed to send IR command: {e}")

def main():
    state = load_state()
    lircclient = lirc.Client()
    desired_source = None

    while True:
        source_1_active = is_audio_source_1_on()
        source_2_active = is_audio_source_2_on()
        log(str(source_1_active) + ' ' + str(source_2_active))
        if source_2_active:
            desired_source = "source_2"
            select_command = IR_SELECT_SOURCE_2
            state["last_active"] = time.time()
        elif source_1_active:
            desired_source = "source_1"
            select_command = IR_SELECT_SOURCE_1
            state["last_active"] = time.time()
        else:
            if desired_source != None:
              log("Source stopped playing")
            desired_source = None
            select_command = None

        if desired_source:
            if not state["amp_on"]:
                log("Turning amplifier ON")
                send_ir_command(lircclient, IR_POWER_TOGGLE)
                state["amp_on"] = True
                time.sleep(5)

            if state["current_source"] != desired_source:
                log(f"Switching to {desired_source}")
                send_ir_command(lircclient, select_command)
                state["current_source"] = desired_source
                save_state(state)

        elif state["amp_on"] and (time.time() - state["last_active"]) > SHUTDOWN_DELAY:
            log("Turning amplifier OFF")
            send_ir_command(lircclient, IR_POWER_TOGGLE)
            state["amp_on"] = False
            state["current_source"] = None
            save_state(state)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()