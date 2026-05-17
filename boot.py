from machine import UART, Pin
import machine
import network
import urequests
import time
import struct
import gc

# ── Import Secrets ─────────────────────────────────────────────────
import secrets  # This loads the secrets.py file from the ESP32

# ── OTA Update Settings ────────────────────────────────────────────
CURRENT_VERSION = 0.9 
VERSION_URL = "https://github.com/harris84firefox/esp32-power-meter/blob/main/version.txt"
UPDATE_URL  = "https://github.com/harris84firefox/esp32-power-meter/blob/main/boot.py"

# ── WiFi & ThingSpeak (Using imported secrets) ─────────────────────
WIFI_SSID      = secrets.WIFI_SSID
WIFI_PASSWORD  = secrets.WIFI_PASSWORD
THINGSPEAK_KEY = secrets.THINGSPEAK_KEY
THINGSPEAK_URL = "https://api.thingspeak.com/update"

# ── UART / Modbus ──────────────────────────────────────────────────
uart = UART(1, baudrate=9600, tx=5, rx=4, timeout=200)
SLAVE_ADDR = 0x01

# ── ThingSpeak Field Map (max 8) ───────────────────────────────────
# field1=Power Total, field2=Power L1, field3=Power L2, field4=Power L3
# field5=Voltage L1,  field6=Current L1, field7=PF Total, field8=Frequency
THINGSPEAK_FIELDS = [
    "Power Total",
    "Power L1",
    "Power L2",
    "Power L3",
    "Voltage L1",
    "Energy kWh"
]

# ── CRC ───────────────────────────────────────────────────────────
def calculate_crc(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc

# ── Bulk Register Read ────────────────────────────────────────────
def read_bulk(start_addr, count):
    request = bytearray([
        SLAVE_ADDR, 0x03,
        (start_addr >> 8) & 0xFF, start_addr & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF
    ])
    crc = calculate_crc(request)
    request += bytearray([crc & 0xFF, (crc >> 8) & 0xFF])

    while uart.any():   # flush stale bytes
        uart.read()

    uart.write(request)
    time.sleep_ms(100)
    response = uart.read()

    if response is None:
        return None
    expected = 3 + count * 2 + 2
    if len(response) < expected:
        return None
    rcv_crc = (response[-1] << 8) | response[-2]
    if calculate_crc(response[:-2]) != rcv_crc:
        return None

    regs = []
    for i in range(count):
        offset = 3 + i * 2
        regs.append(struct.unpack('>H', response[offset:offset+2])[0])
    return regs

def signed(val):
    return val if val < 0x8000 else val - 0x10000

# ── Read All Meter Data ───────────────────────────────────────────
def read_meter():
    readings = {}

    # Block 1: registers 0x00–0x1A (27 registers — voltage to frequency)
    b1 = read_bulk(0x0000, 27)
    if b1:
        readings["Voltage L1"]   = b1[0x00] * 0.1
        readings["Voltage L2"]   = b1[0x01] * 0.1
        readings["Voltage L3"]   = b1[0x02] * 0.1
        readings["Current L1"]   = b1[0x03] * 0.01
        readings["Current L2"]   = b1[0x04] * 0.01
        readings["Current L3"]   = b1[0x05] * 0.01
        readings["Power Total"]  = signed(b1[0x07]) * 1.0
        readings["Power L1"]     = signed(b1[0x08]) * 1.0
        readings["Power L2"]     = signed(b1[0x09]) * 1.0
        readings["Power L3"]     = signed(b1[0x0A]) * 1.0
        readings["React Total"]  = signed(b1[0x0B]) * 1.0
        readings["Appar Total"]  = b1[0x0F] * 1.0
        readings["PF Total"]     = b1[0x13] * 0.001
        readings["PF L1"]        = b1[0x14] * 0.001
        readings["PF L2"]        = b1[0x15] * 0.001
        readings["PF L3"]        = b1[0x16] * 0.001
        readings["Frequency"]    = b1[0x1A] * 0.01
    else:
        print("  ✗ Block 1 failed")

    # Block 2: 0x001D–0x001E (32-bit energy)
    b2 = read_bulk(0x001D, 2)
    if b2:
        readings["Energy kWh"] = ((b2[0] << 16) | b2[1]) * 0.01
    else:
        print("  ✗ Energy read failed")

    return readings

# ── WiFi ──────────────────────────────────────────────────────────
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan
    print(f"Connecting to {WIFI_SSID}...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(20):
        if wlan.isconnected():
            break
        time.sleep(0.5)
    if wlan.isconnected():
        print(f"WiFi OK — {wlan.ifconfig()[0]}")
    else:
        print("WiFi FAILED — will keep retrying")
    return wlan

# ── ThingSpeak Upload ─────────────────────────────────────────────
def upload(readings):
    params = f"api_key={THINGSPEAK_KEY}"
    for i, name in enumerate(THINGSPEAK_FIELDS, start=1):
        val = readings.get(name)
        if val is not None:
            params += f"&field{i}={val:.4f}"
    try:
        r = urequests.get(f"{THINGSPEAK_URL}?{params}", timeout=10)
        ok = r.text.strip() != "0"
        r.close()
        return ok
    except Exception as e:
        print(f"  Upload error: {e}")
        return False

# ── OTA Update Function ───────────────────────────────────────────
def check_for_updates():
    print(f"Checking for updates... (Current Version: {CURRENT_VERSION})")
    try:
        # 1. Fetch the version number from GitHub
        gc.collect() # Free up RAM before making HTTPS requests
        response = urequests.get(VERSION_URL, timeout=10)
        github_version = float(response.text.strip())
        response.close()
        
        # 2. Compare versions
        if github_version > CURRENT_VERSION:
            print(f"New version {github_version} found! Downloading...")
            
            # 3. Download the new code
            response = urequests.get(UPDATE_URL, timeout=15)
            new_code = response.text
            response.close()
            
            # 4. Overwrite the local main.py
            with open('main.py', 'w') as f:
                f.write(new_code)
                
            print("Update complete. Rebooting ESP32...")
            time.sleep(2)
            machine.reset() # Restart the board to run the new code
            
        else:
            print("No updates available. Running current code.")
            
    except Exception as e:
        print(f"OTA check failed: {e}")

# ── Main Loop ─────────────────────────────────────────────────────
def main():
    print("EA777 + ThingSpeak — ESP32-C3")
    wlan = connect_wifi()
    
    # Run the OTA check right after WiFi connects, before the main loop starts
    if wlan.isconnected():
        check_for_updates()

    while True:
        # Reconnect WiFi if dropped
        if not wlan.isconnected():
            print("WiFi dropped — reconnecting...")
            wlan = connect_wifi()

        t = time.localtime()
        print(f"\n{'='*40}")
        print(f"  {t[3]:02d}:{t[4]:02d}:{t[5]:02d}  {t[2]:02d}/{t[1]:02d}/{t[0]}")
        print(f"{'='*40}")

        readings = read_meter()

        # Print all readings
        for name, val in readings.items():
            print(f"  {name:<18}: {val:.3f}")

        # Upload
        if readings:
            if upload(readings):
                print("  ✓ ThingSpeak OK")
            else:
                print("  ✗ ThingSpeak failed")

        print(f"{'='*40}")
        time.sleep(15)

if __name__ == "__main__":
    main()
