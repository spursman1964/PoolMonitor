from smbus2 import SMBus, i2c_msg
import time

PH_ADDRESS = 0x63

def atlas_command(bus, address, command, delay=1.0):
    # Atlas EZO expects ASCII command bytes
    write = i2c_msg.write(address, command.encode("ascii"))
    bus.i2c_rdwr(write)

    time.sleep(delay)

    read = i2c_msg.read(address, 32)
    bus.i2c_rdwr(read)

    data = list(read)
    status = data[0]
    text = "".join(chr(x) for x in data[1:] if x not in (0, 255))

    return status, text

with SMBus(1) as bus:
    status, result = atlas_command(bus, PH_ADDRESS, "R", delay=1.0)

if status == 1:
    print("pH =", result)
else:
    print("Error, status =", status, "response =", result)
