# 1. Continuous spinning (keeps going until you stop it)
# Replace the main loop with this:
while True:
    for pin in range(len(motor_pins)):
        motor_pins[pin].value = bool(step_sequence[motor_step_counter][pin])
    if direction:
        motor_step_counter = (motor_step_counter - 1) % 8
    else:
        motor_step_counter = (motor_step_counter + 1) % 8
    time.sleep(step_sleep)