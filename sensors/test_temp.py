"""
Standalone RTD diagnostic. Run this directly on the Pi to see exactly
what the EZO-RTD circuit is returning, including the raw bytes if the
parsed value looks wrong.

Usage:
    python3 test_rtd.py
"""
from smbus2 import SMBus, i2c_msg
import time

RTD_ADDRESS = 0x66


def raw_read():
    with SMBus(1) as bus:
        print(f"Sending 'R' command to address 0x{RTD_ADDRESS:02x}...")
        bus.i2c_rdwr(i2c_msg.write(RTD_ADDRESS, b"R"))

        print("Waiting 1.0s for the circuit to process the reading...")
        time.sleep(1.0)

        read = i2c_msg.read(RTD_ADDRESS, 32)
        bus.i2c_rdwr(read)

        data = list(read)

    print(f"Raw bytes received: {data}")

    status = data[0]
    print(f"Status byte: {status} "
          f"({'success' if status == 1 else 'NOT success'})")

    value_str = "".join(
        chr(x) for x in data[1:]
        if x not in (0, 255)
    )
    print(f"Decoded string: {value_str!r}")

    if status == 1:
        try:
            value = float(value_str)
            print(f"Parsed temperature: {value} C")
        except ValueError:
            print("Could not parse the decoded string as a float.")
    else:
        print("Status byte was not 1 (success), so this reading is not valid.")
        print("Common status codes: 2 = syntax error, 254 = still processing, 255 = no data to send")


if __name__ == "__main__":
    raw_read()
