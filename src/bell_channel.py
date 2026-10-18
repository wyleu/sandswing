# bell_channel.py -- one channel (Y).  Holds references only: its sense and
# drive paths (shared), and, once concentrator attaches them, its laser
# PWMOut (.laser) and sense input (.sense_pin).


class Bell:
    def __init__(self, index, y, tower, sense=None, drive=None):
        self.index = int(index)
        self.y = int(y)
        self.tower = tower
        self.sense = sense
        self.drive = drive
        self.laser = None
        self.sense_pin = None

    def read_sense(self):
        return self.sense.read(self.y)

    def set_drive(self, duty, wait=True):
        return self.drive.set(self.y, duty, wait)

    def readback(self):
        return self.drive.readback(self.y)

    def __repr__(self):
        return "Bell(%d, y=%d)" % (self.index, self.y)
