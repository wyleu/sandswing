# adc_avg.py -- average n ADC reads. Leaf.


def avg(adc, n):
    s = 0
    for _ in range(n):
        s += adc.value
    return s // n
