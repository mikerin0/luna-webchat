# Luna Robot

6DOF Hiwonder arm with Raspberry Pi 5 + Hailo8 AI Hat + Ubuntu Ollama brain.

## Structure
- `pi/robotarm/` - Raspberry Pi 5 code (object detection, arm control, voice)
- `ubuntu/luna-webchat/` - Ubuntu PC code (web interface, Ollama integration)

## Setup

### Pi
```bash
source /home/arm/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/activate
cd pi/robotarm
python main.py
```

### Ubuntu
Runs automatically on boot. Access at https://172.31.31.106:3010/

## Status
- Pi: Working ✓
- Ubuntu: Working ✓
- Integration: In progress
