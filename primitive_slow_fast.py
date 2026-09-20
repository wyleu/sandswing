# 3. Variable speed (slow → fast → slow)
import math

for i in range(step_count):
    # speed varies with a sine wave
    speed = 0.001 + 0.004 * (1 + math.sin(i / 200))
    for pin in range(len(motor_pins)):
        motor_pins[pin].value = bool(step_sequence[motor_step_counter][pin])
    motor_step_counter = (motor_step_counter + 1) % 8
    time.sleep(speed)