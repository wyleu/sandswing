class Pin:
    def __init__(self, gp):
        self.gp = gp
    def __repr__(self):
        return "board.GP%d" % self.gp

for _n in range(30):
    globals()["GP%d" % _n] = Pin(_n)
LED = Pin(99)
