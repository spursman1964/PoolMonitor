from smbus2 import SMBus, i2c_msg
import time

EC_ADDRESS = 0x64

def read_ec(temperature_c=None):
    """
    Read conductivity from the Atlas Scientific EZO-EC circuit.

    If temperature_c is provided, sends a temperature compensation command
    first so the EC circuit can correct for temperature effects on conductivity.
    This improves accuracy significantly -- always pass temperature if available.

    Returns a dict with:
        ec          -- electrical conductivity in µS/cm
        tds         -- total dissolved solids in ppm
        salinity    -- salinity in PSU (practical salinity units)
        sg          -- specific gravity (relative to pure water at 4°C)
    """
    with SMBus(1) as bus:

        # Send temperature compensation if we have a reading
        if temperature_c is not None:
            temp_cmd = f"T,{temperature_c:.2f}".encode()
            bus.i2c_rdwr(i2c_msg.write(EC_ADDRESS, temp_cmd))
            time.sleep(0.3)

        # Request a reading
        bus.i2c_rdwr(i2c_msg.write(EC_ADDRESS, b"R"))
        time.sleep(1.0)

        read = i2c_msg.read(EC_ADDRESS, 48)
        bus.i2c_rdwr(read)

        data = list(read)

    status = data[0]

    if status != 1:
        raise RuntimeError(f"EZO-EC returned status {status}")

    value_str = "".join(
        chr(x) for x in data[1:]
        if x not in (0, 255)
    )

    # EZO-EC returns comma-separated values: EC,TDS,SAL,SG
    # e.g. "1413.56,706.14,0.70,1.0005"
    parts = value_str.strip().split(",")

    if len(parts) < 4:
        raise RuntimeError(f"EZO-EC returned unexpected format: {value_str!r}")

    return {
        "ec":       float(parts[0]),   # µS/cm
        "tds":      float(parts[1]),   # ppm
        "salinity": float(parts[2]),   # PSU
        "sg":       float(parts[3]),   # specific gravity
    }


def read_salinity_ppm(temperature_c=None):
    """
    Convenience wrapper that returns just the salinity value in ppm (TDS),
    matching the salinity_ppm column in the database.
    """
    result = read_ec(temperature_c=temperature_c)
    return result["tds"]
