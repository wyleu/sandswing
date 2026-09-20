# 2. Oscillate (back and forth)

steps_per_direction = 1024   # quarter turn each way, adjust as you like

while True:
    # one direction
    for _ in range(steps_per_direction):
        for pin in range(len(motor_pins)):
            motor_pins[pin].value = bool(step_sequence[motor_step_counter][pin])
        motor_step_counter = (motor_step_counter + 1) % 8
        time.sleep(step_sleep)

    # other direction
    for _ in range(steps_per_direction):
        for pin in range(len(motor_pins)):
            motor_pins[pin].value = bool(step_sequence[motor_step_counter][pin])
        motor_step_counter = (motor_step_counter - 1) % 8
        time.sleep(step_sleep)