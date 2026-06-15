from smbus2 import SMBus

bus = SMBus(1)

devices = []

for address in range(0x03,0x078):
    try:
        bus.read_byte(address)
        devices.append(hex(address))
    except Exception:
        pass

print("Devices found:", devices)

bus.close()