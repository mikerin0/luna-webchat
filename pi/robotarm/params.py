# params.py
# Global settable parameters adjustable and savable by the GUI

PARAMS = {
    # Example: 'speed': 1000,
}

def get_param(name, default=None):
    return PARAMS.get(name, default)

def set_param(name, value):
    PARAMS[name] = value
