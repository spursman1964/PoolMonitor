from smbus2 import SMBus, i2c_msg
import time

PH_ADDRESS = 0x63

def read_ph():
    with SMBus(1) as bus:
        bus.i2c_rdwr(i2c_msg.write(PH_ADDRESS, b"R"))

        time.sleep(1.0)

        read = i2c_msg.read(PH_ADDRESS, 32)
        bus.i2c_rdwr(read)

        data = list(read)

    status = data[0]

    if status != 1:
        raise RuntimeError(f"EZO returned status {status}")

    value = "".join(
        chr(x) for x in data[1:]
        if x not in (0, 255)
    )

    return float(value)
