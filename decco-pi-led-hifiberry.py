#!/usr/bin/python3 
 
import time
import os
from datetime import datetime
import subprocess

"""
Peachtree Decco Remote Automator

This program automatically controls the Peachtree Decco amplifier based on music playback status.
- If music is detected, it turns the amplifier on.
- If music stops playing for a certain period, it turns the amplifier off.
- The music status is fetched remotely from a Raspberry Pi via SSH.
- The program logs events with timestamps and can send IR commands to switch inputs.


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
NO_SOUND_THRESHOLD = 60 * 60  # Turn off amp after 60 minutes of no sound

def log_message(message):
    """Logs a message with a timestamp."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}")
    
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
    """Turns the amplifier on and logs the event."""
    log_message("Amplifier turning ON")

    volume_ticks = round(HIFIBERRY_DEFAULT_VOLUME + random.gauss(0, 0.1 * HIFIBERRY_DEFAULT_VOLUME))
    write_amplifier_status(True)
    AMPLIFIER_ON_CMD = "irsend SEND_ONCE decco ON "+HIFIBERRY_SOUND_CHANNEL+"  && sleep 20 && irsend SEND_ONCE decco -# "+str(volume_ticks)+" VOL_UP "
    log_message(AMPLIFIER_ON_CMD)
    os.system(AMPLIFIER_ON_CMD)

    log_message("Amplifier turned ON")

def turn_amplifier_off():
    """Turns the amplifier off and logs the event."""
    AMPLIFIER_OFF_CMD = "irsend SEND_ONCE decco OFF"  # Command to turn amplifier off
    os.system(AMPLIFIER_OFF_CMD)
    write_amplifier_status(False)
    log_message("Amplifier turned OFF")

def amplifier_lightshow():
    """ Lightshow on the DECCO to indicate that this program has restarted"""
    channels = ("USB","COAX","OPT","AUX1","AUX2")
    for channel in channels:
        if channel != HIFIBERRY_SOUND_CHANNEL:
          os.system("irsend SEND_ONCE decco "+channel)
          time.sleep(0.1)
    # return to the output channel, so that music keeps playing.
    os.system("irsend SEND_ONCE decco "+HIFIBERRY_SOUND_CHANNEL)


def process_music_status(amplifier_on, no_sound_time):
    """
    Processes the status of music playback, turning the amplifier on/off and 
    sending IR commands based on whether music is playing or stopped.
    """
    if read_pcm_status_local():
        # Music is playing
        if no_sound_time != 0:
            log_message("Music started playing")
            os.system("irsend SEND_ONCE decco AUX1")

        if not amplifier_on:
            turn_amplifier_on()
            amplifier_on = True
        no_sound_time = 0
    else:
        # No music
        if no_sound_time == 0:
            log_message("Music stopped playing")

        # change to a different channel to indicate the missing sound
        if (NO_SOUND_THRESHOLD * 0.03) < no_sound_time < (NO_SOUND_THRESHOLD * 0.03 + 1.5 *CHECK_INTERVAL ):
            log_message("Music stopped playing for a while")
            os.system("irsend SEND_ONCE decco USB")

        # switch amplfier to mute, to warn the user of the impending "power off" of the amplifier 
        if (NO_SOUND_THRESHOLD * 0.60) < no_sound_time < (NO_SOUND_THRESHOLD * 0.60 + 1.5 *CHECK_INTERVAL ):
            log_message("Music stopped playing a long time ago")
            os.system("irsend SEND_ONCE decco COAX")

        no_sound_time += CHECK_INTERVAL
        if amplifier_on and no_sound_time >= NO_SOUND_THRESHOLD:
            turn_amplifier_off()
            amplifier_on = False

    return amplifier_on, no_sound_time

def main():
    """Main loop for monitoring music playback and controlling the amplifier."""
    amplifier_on = read_amplifier_status()
    no_sound_time = 0 if amplifier_on else NO_SOUND_THRESHOLD  # Start with amp off if needed

    log_message(f"Loop started with amplifier {'ON' if amplifier_on else 'OFF'}")

    amplifier_lightshow()

    while True:
        amplifier_on, no_sound_time = process_music_status(amplifier_on, no_sound_time)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()