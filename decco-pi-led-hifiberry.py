#!/usr/bin/python3

import time
import os
import json
import ssl
import asyncio
import threading
from datetime import datetime
import logging

import websockets

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

"""
Peachtree Decco Remote Automator

This program automatically controls the Peachtree Decco amplifier based on music playback status.
- If music is detected, it turns the amplifier on.
- If music stops playing for a certain period, it turns the amplifier off.
- The music status is read locally from the HiFiBerry PCM status file.
- The program logs events with timestamps and can send IR commands to switch inputs.

It also listens to Home Assistant over a WebSocket connection and toggles the
amplifier when the configured event entity fires (see EVENT_ENTITY below).
"Manual override wins": a manual OFF while music is playing forces the amp off
and suppresses auto-on until playback stops and restarts; a manual ON forces it
on.

- USB
- COAX
- OPT
- AUX1
- AUX2
"""

HIFIBERRY_SOUND_CHANNEL = "AUX1" # Standard sound input
HIFIBERRY_DEFAULT_VOLUME = 18 # measured in IR VOL_UP ticks

STATUS_FILE = "/var/tmp/peachtree_amplifier_status.txt"  # Path to save amplifier status

CHECK_INTERVAL = 10  # Check every 10 seconds
NO_SOUND_THRESHOLD = 2 * 60 * 60  # (seconds) Turn off amp after 2 hours * 3600 seconds of no sound

# Home Assistant WebSocket listener config. All three are read from the systemd
# EnvironmentFile /etc/decco/ha-listener.env -- see ha-listener.env.example.
# No defaults on purpose: the URL and the entity id are site-specific, and the
# token is a secret. If any is unset the listener stays disabled and the
# playback monitor runs on its own.
# Point HA_WS_URL at a hostname your HA certificate actually matches, so TLS
# verification can stay ON -- the channel carries a long-lived bearer token.
HA_WS_URL = os.environ.get("HA_WS_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")
EVENT_ENTITY = os.environ.get("EVENT_ENTITY")

# Shared state touched by both the monitor loop (main thread) and the HA
# WebSocket listener (background thread). All access goes through state_lock so
# only one thread ever drives IR / writes the status file at a time.
state_lock = threading.Lock()
state = {
    "amplifier_on": False,
    "no_sound_time": 0,
    # When True, the user manually turned the amp off while music was playing;
    # auto-on is suppressed until playback stops (which re-arms) and restarts.
    "manual_off": False,
}


def read_pcm_status_local():
    """
    Checks the PCM status on the local device.
    Returns True if the status file starts with 'state: RUNNING', indicating that music is playing.
    """
    status_path = '/proc/asound/card0/pcm0p/sub0/status'
    try:
        with open(status_path, 'r') as f:
            first_line = f.readline().strip()
            return first_line == 'state: RUNNING'
    except (FileNotFoundError, IOError):
        logging.warning("Amplifier state file not found or not accesible.")
        return False


def read_amplifier_status():
    """Reads the current amplifier status from disk."""
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, 'r') as f:
            return f.read().strip() == 'ON'
    return False


def write_amplifier_status(on):
    """Writes the amplifier status (ON/OFF) to disk."""
    with open(STATUS_FILE, 'w') as f:
        f.write('ON' if on else 'OFF')


def turn_amplifier_on():
    import random
    """Turns the amplifier on and logs the event. Caller must hold state_lock."""
    logging.info("Amplifier turning ON")

    volume_ticks = round(HIFIBERRY_DEFAULT_VOLUME + random.gauss(0, 0.1 * HIFIBERRY_DEFAULT_VOLUME))
    write_amplifier_status(True)
    AMPLIFIER_ON_CMD = "irsend SEND_ONCE decco ON "+HIFIBERRY_SOUND_CHANNEL+"  && sleep 20 && irsend SEND_ONCE decco -# "+str(volume_ticks)+" VOL_UP "
    logging.info(AMPLIFIER_ON_CMD)
    os.system(AMPLIFIER_ON_CMD)

    logging.info("Amplifier turned ON")


def turn_amplifier_off():
    """Turns the amplifier off and logs the event. Caller must hold state_lock."""
    AMPLIFIER_OFF_CMD = "irsend SEND_ONCE decco OFF"  # Command to turn amplifier off
    os.system(AMPLIFIER_OFF_CMD)
    write_amplifier_status(False)
    logging.info("Amplifier turned OFF")


def amplifier_lightshow():
    """ Lightshow on the DECCO to indicate that this program has restarted"""
    channels = ("USB","COAX","OPT","AUX1","AUX2")
    for channel in channels:
        if channel != HIFIBERRY_SOUND_CHANNEL:
          os.system("irsend SEND_ONCE decco "+channel)
          time.sleep(0.1)
    # return to the output channel, so that music keeps playing.
    os.system("irsend SEND_ONCE decco "+HIFIBERRY_SOUND_CHANNEL)


def handle_toggle():
    """
    Toggle the amplifier in response to an HA event. "Manual override wins":
    - amp ON  -> turn OFF; if music is currently playing, set manual_off so the
      monitor loop won't immediately turn it back on.
    - amp OFF -> turn ON; clear manual_off and reset the silence timer.
    Runs on the WebSocket thread, so it takes state_lock.
    """
    with state_lock:
        playing = read_pcm_status_local()
        if state["amplifier_on"]:
            turn_amplifier_off()
            state["amplifier_on"] = False
            state["manual_off"] = playing
            logging.info("Manual toggle -> OFF (suppress auto-on=%s)", state["manual_off"])
        else:
            turn_amplifier_on()
            state["amplifier_on"] = True
            state["manual_off"] = False
            state["no_sound_time"] = 0
            logging.info("Manual toggle -> ON")


def monitor_once():
    """
    One iteration of the playback monitor: turn the amplifier on/off and send IR
    commands based on whether music is playing. Mutates shared state under lock.
    """
    with state_lock:
        if read_pcm_status_local():
            # Music is playing
            if state["no_sound_time"] != 0:
                logging.info("Music started playing")
                os.system("irsend SEND_ONCE decco AUX1")

            if state["manual_off"]:
                # User forced the amp off while music kept playing -> stay off
                # until playback actually stops and restarts.
                pass
            elif not state["amplifier_on"]:
                turn_amplifier_on()
                state["amplifier_on"] = True
            state["no_sound_time"] = 0
        else:
            # No music. A real stop re-arms any manual override.
            if state["manual_off"]:
                logging.info("Playback stopped; clearing manual override")
                state["manual_off"] = False

            if state["no_sound_time"] == 0:
                logging.info("Music stopped playing")

            # change to a different channel to indicate the missing sound
            if (NO_SOUND_THRESHOLD * 0.03) < state["no_sound_time"] < (NO_SOUND_THRESHOLD * 0.03 + 1.5 * CHECK_INTERVAL):
                logging.info("Music stopped playing for a while")
                os.system("irsend SEND_ONCE decco USB")

            # switch amplifier to mute, to warn the user of the impending "power off" of the amplifier
            if (NO_SOUND_THRESHOLD * 0.60) < state["no_sound_time"] < (NO_SOUND_THRESHOLD * 0.60 + 1.5 * CHECK_INTERVAL):
                logging.info("Music stopped playing a long time ago")
                os.system("irsend SEND_ONCE decco COAX")

            state["no_sound_time"] += CHECK_INTERVAL
            if state["amplifier_on"] and state["no_sound_time"] >= NO_SOUND_THRESHOLD:
                turn_amplifier_off()
                state["amplifier_on"] = False


def automatic_delay_length():
    """
    Automatically sets the sound threshold based on the current time.
    """
    current_time = datetime.now()
    if current_time.hour >= 22 or current_time.hour < 4:
        return 60 * 60 * 0.5  # 30 minutes  during night
    else:
        return 60 * 60 * 2  # Default threshold


async def ha_ws_listen():
    """
    Maintain a Home Assistant WebSocket connection and call handle_toggle() each
    time the configured event entity fires. Reconnects with backoff; relies on
    websockets' ping/pong keepalive to notice silently-dropped sockets (e.g. an
    HA restart that doesn't send a clean close) within ~tens of seconds.
    """
    missing = [n for n, v in (("HA_WS_URL", HA_WS_URL), ("HA_TOKEN", HA_TOKEN),
                              ("EVENT_ENTITY", EVENT_ENTITY)) if not v]
    if missing:
        logging.error("%s not set; HA WebSocket listener disabled", ", ".join(missing))
        return

    # Verifying TLS context for wss (default CA store; the hostname in HA_WS_URL
    # must match the certificate HA presents).
    ssl_ctx = ssl.create_default_context() if HA_WS_URL.startswith("wss://") else None

    backoff = 1
    while True:
        try:
            async with websockets.connect(
                HA_WS_URL, ssl=ssl_ctx, ping_interval=20, ping_timeout=20, close_timeout=5
            ) as ws:
                # Auth handshake.
                await ws.recv()  # auth_required
                await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                auth = json.loads(await ws.recv())
                if auth.get("type") != "auth_ok":
                    logging.error("HA auth failed: %s", auth)
                    await asyncio.sleep(30)
                    continue
                logging.info("HA WebSocket auth_ok")

                # Subscribe to state changes of the toggle event entity.
                sub_id = 1
                await ws.send(json.dumps({
                    "id": sub_id,
                    "type": "subscribe_trigger",
                    "trigger": {"platform": "state", "entity_id": EVENT_ENTITY},
                }))
                logging.info("Subscribed to %s", EVENT_ENTITY)
                backoff = 1  # connection healthy -> reset backoff

                async for raw in ws:
                    data = json.loads(raw)
                    if data.get("type") == "event" and data.get("id") == sub_id:
                        trig = data["event"].get("variables", {}).get("trigger", {})
                        from_value = (trig.get("from_state") or {}).get("state")
                        new_state = trig.get("to_state") or {}
                        to_value = new_state.get("state")
                        event_type = (new_state.get("attributes") or {}).get("event_type")
                        # An ESP/HA reconnect marks the entity `unavailable`, then
                        # RESTORES it to its last event timestamp -- a state change
                        # carrying the OLD event_type but no real button press. A
                        # genuine long-press always transitions FROM a valid state
                        # (a prior timestamp, or "unknown" before the first press) TO
                        # a newer timestamp. So ignore anything coming from
                        # "unavailable" or with an invalid target state.
                        if (event_type is None
                                or to_value in (None, "unavailable", "unknown")
                                or from_value == "unavailable"):
                            logging.info(
                                "Ignoring reconnect/restore transition %s -> %s (event_type=%s)",
                                from_value, to_value, event_type)
                            continue
                        logging.info("HA toggle event fired (event_type=%s)", event_type)
                        handle_toggle()
        except Exception as e:
            logging.warning("HA WebSocket error: %s; reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start_ws_thread():
    """Run the asyncio WebSocket listener on a daemon background thread."""
    threading.Thread(
        target=lambda: asyncio.run(ha_ws_listen()),
        name="ha-ws",
        daemon=True,
    ).start()


def main():
    """Main loop for monitoring music playback and controlling the amplifier."""
    with state_lock:
        state["amplifier_on"] = read_amplifier_status()
        state["no_sound_time"] = 0 if state["amplifier_on"] else NO_SOUND_THRESHOLD

    logging.info(f"Loop started with amplifier {'ON' if state['amplifier_on'] else 'OFF'}")

    amplifier_lightshow()
    start_ws_thread()

    while True:
        monitor_once()
        automatic_delay_length()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
