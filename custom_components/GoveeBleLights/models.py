from enum import IntEnum

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

    # default min/max kelvin values will display to frontend for all lights
    # set individual models' min/max kelvin to its true values
    # setting kelvin outside of an individual model's range will convert to rgb approximation
    MODELS = {
        "default": {
            led_mode: LedMode.MODE_2, 
            brightness_max: 255,
            min_kelvin: 1000,
            max_kelvin: 6500,
        },
        "H6008": {
            led_mode: LedMode.MODE_D, 
            brightness_max: 100,
            min_kelvin: 2700,
            max_kelvin: 6500,
        },
        "H6046": {
            led_mode: LedMode.MODE_1501, 
            brightness_max: 100,
            min_kelvin: 1500,
            max_kelvin: 6500,
        },
        "H6072": {
            led_mode: LedMode.MODE_1501, 
            brightness_max: 100,
            min_kelvin: 0,
            max_kelvin: 0,
        },
        "H6076": {
            led_mode: LedMode.MODE_1501, 
            brightness_max: 100,
            min_kelvin: 3300,
            max_kelvin: 4300,
        },
        
    }

    @staticmethod
    def get(model, key):
        if model in ModelInfo.MODELS and ModelInfo.MODELS[model][key]:
            return ModelInfo.MODELS[model][key]
        else:
            return ModelInfo.MODELS["default"][key]

    @staticmethod
    def get_led_mode(model):
        if model in ModelInfo.MODELS and ModelInfo.MODELS[model]["led_mode"]:
            return ModelInfo.MODELS[model]["led_mode"]
        else:
            return ModelInfo.MODELS["default"]["led_mode"]
    
    @staticmethod
    def get_brightness_max(model):
        if model in ModelInfo.MODELS and ModelInfo.MODELS[model]["brightness_max"]:
            return ModelInfo.MODELS[model]["brightness_max"]
        else:
            return ModelInfo.MODELS["default"]["brightness_max"]
    

