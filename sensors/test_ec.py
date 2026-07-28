"""
Standalone EZO-EC diagnostic. Run directly on the Pi to confirm
the EC circuit is responding and returning sensible values.

Usage:
    python3 test_ec.py

The probe should be in a liquid (calibration solution or pool water)
for a meaningful reading. In air it will still respond but the value
will be meaningless.
"""
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent / "sensors"))

from ec_sensor import read_ec

print("Testing EZO-EC circuit at I2C address 0x64...")
print("(Probe should be submerged for a meaningful reading)\n")

for attempt in range(1, 4):
    try:
        print(f"Attempt {attempt}...")
        result = read_ec(temperature_c=25.0)  # assume 25C for test
        print(f"  EC:       {result['ec']:.2f} µS/cm")
        print(f"  TDS:      {result['tds']:.2f} ppm")
        print(f"  Salinity: {result['salinity']:.2f} PSU")
        print(f"  SG:       {result['sg']:.4f}")
        print("\nEC circuit is responding correctly.")
        break
    except Exception as e:
        print(f"  ERROR: {e}")
        if attempt < 3:
            print("  Retrying in 2 seconds...")
            time.sleep(2)
        else:
            print("\nEC circuit failed to respond after 3 attempts.")
            print("Check:")
            print("  - EZO-EC is seated in the Tentacle slot")
            print("  - Circuit is in I2C mode (blue LED)")
            print("  - i2cdetect -y 1 shows 0x64")
