from smbus2 import SMBus, i2c_msg
import time

PH_ADDRESS = 0x63

with SMBus(1) as bus:
    bus.i2c_rdwr(i2c_msg.write(PH_ADDRESS, b"Cal,mid,7.00"))
    time.sleep(1.5)

    read = i2c_msg.read(PH_ADDRESS, 32)
    bus.i2c_rdwr(read)

    data = list(read)

print(data)
print("".join(chr(x) for x in data[1:] if x > 0))
