from machine import ADC, Pin


class Thermistor:
    def __init__(self, pin):
        self.adc = ADC(Pin(pin))
        if hasattr(self.adc, "atten"):
            self.adc.atten(ADC.ATTN_11DB)

    def read_raw(self):
        if hasattr(self.adc, "read_u16"):
            return self.adc.read_u16()
        return self.adc.read()

    def read_temperature_c(self):
        return None

    def read(self):
        return {
            "raw": self.read_raw(),
            "c": self.read_temperature_c(),
        }
