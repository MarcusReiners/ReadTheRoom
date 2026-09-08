import sys

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

ser = serial.Serial(port, baud, timeout=1)
print(f"Listening raw on {port} (Ctrl+C to quit)...")

try:
    while True:
        line = ser.readline()
        if line:
            print(line)
except KeyboardInterrupt:
    pass
