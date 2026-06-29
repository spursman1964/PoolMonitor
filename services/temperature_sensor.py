from smbus2 import SMBus, i2c_msg
import time

RTD_ADDRESS = 0x66

def read_temperature():
    with SMBus(1) as bus:
        bus.i2c_rdwr(i2c_msg.write(RTD_ADDRESS, b"R"))

        time.sleep(1.0)

        read = i2c_msg.read(RTD_ADDRESS, 32)
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
