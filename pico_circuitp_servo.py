import board
import digitalio
import time

# GPIO pins for Pico 2W
in1 = digitalio.DigitalInOut(board.GP18)
in2 = digitalio.DigitalInOut(board.GP19)
in3 = digitalio.DigitalInOut(board.GP20)
in4 = digitalio.DigitalInOut(board.GP21)

in1.direction = digitalio.Direction.OUTPUT
in2.direction = digitalio.Direction.OUTPUT
in3.direction = digitalio.Direction.OUTPUT
in4.direction = digitalio.Direction.OUTPUT

# careful lowering this, at some point you run into the mechanical limitation of how quick your motor can move
step_sleep = 0.01
step_count = 4096  # 5.625*(1/64) per step, 4096 steps is 360°
direction = False  # True for clockwise, False for counter-clockwise

# defining stepper motor sequence
step_sequence = [[1,0,0,1],
                 [1,0,0,0],
                 [1,1,0,0],
                 [0,1,0,0],
                 [0,1,1,0],
                 [0,0,1,0],
                 [0,0,1,1],
                 [0,0,0,1]]

# initializing
in1.value = False
in2.value = False
in3.value = False
in4.value = False

motor_pins = [in1, in2, in3, in4]
motor_step_counter = 0

def cleanup():
    in1.value = False
    in2.value = False
    in3.value = False
    in4.value = False

# the meat
try:
    for i in range(step_count):
        for pin in range(len(motor_pins)):
            motor_pins[pin].value = bool(step_sequence[motor_step_counter][pin])
        if direction:
            motor_step_counter = (motor_step_counter - 1) % 8
        else:
            motor_step_counter = (motor_step_counter + 1) % 8
        time.sleep(step_sleep)
except KeyboardInterrupt:
    cleanup()
    raise SystemExit

cleanup()