import time
import board
import busio

# Initialize I2C bus (change pins if your wiring is different)
# Example for default: GP0 = SDA, GP1 = SCL
i2c = busio.I2C(board.GP5, board.GP4)

# Wait until the I2C bus is ready and lock it
while not i2c.try_lock():
    pass

print("Scanning I2C bus...")

try:
    devices = i2c.scan()
    if devices:
        print("I2C devices found:")
        for device in devices:
            print("  - Address: 0x{:02X} (decimal {})".format(device, device))
        print("Total devices found:", len(devices))
    else:
        print("No I2C devices found.")
finally:
    i2c.unlock()  # Release the bus

# Optional: Scan repeatedly every 5 seconds
while True:
    time.sleep(5)