class LedCommand(IntEnum):
    """ A control command packet's type. """
    POWER      = 0x01
    BRIGHTNESS = 0x04
    COLOR      = 0x05

class LedMode(IntEnum):
    """
    The mode in which a color change happens in.
    
    Currently only manual is supported.
    """
    MODE_2     = 0x02
    MODE_D     = 0x0D
    MODE_1501  = 0x15 # lots more data in the packet, must make exception for this one
    MICROPHONE = 0x06
    SCENES     = 0x05 

class ControlMode(IntEnum):
    COLOR       = 0x01
    TEMPERATURE = 0x02


class ModelInfo:
    """Class to store information about different models of lights."""
    
    def __init__(self):
        self.MODELS = {
            "default": [LedMode.MODE_2, 255],
            "H6008": [LedMode.MODE_D, 255],
            "H6072": [LedMode.MODE_1501, 100]
        }


    def get_led_mode(self, model):
        return self.MODELS.get(model, self.MODELS["default"])[0]
    
    def get_brightness_max(self, model):
        return self.MODELS.get(model, self.MODELS["default"])[1]