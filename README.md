# Just Dance Best - Controller Bridge

A lightweight PC app that bridges physical motion controllers to the [Just Dance Best](https://jdbest.online/) web game. Instead of holding your smartphone, this app lets you use your favorite console controllers to play the game with full motion tracking, haptic feedback (JoyCon), and in-game controls.

## ✨ Features

* **Multi-Controller Support**: 
  * Nintendo Joy-Con (Left and Right)
  * Nintendo Wiimote
  * PlayStation Move (PS3 ZCM1 & PS4 ZCM2)
* **Motion Calibration**: Built-in sensitivity slider (defaults to `1x`) to calibrate scoring, feel free to play around with it.
* **Immersive Haptics**: Triggers custom rumble feedback for Gold Moves ("YEAH") and earning Stars.
* **In-Game Navigation**: 
  * Use the analog stick / D-pad to select your coach.
  * Press the **A button** (or equivalent right-face button) to instantly start the map from the lobby.
* **Account Integration**: Log in with your JDBest account or play as a dynamically generated Guest.

## 🛠️ Prerequisites
* **Bluetooth**: Your PC must have Bluetooth to pair the controllers (Wiimote, Joy-Con, PS Move).
* Windows, macOS, or Linux.

## 📦 Python Imports
   ```bash
   pip install customtkinter Pillow requests websockets hid joycon-python
```

## Disclaimer
This is an unofficial app and is not developed by the Just Dance Best team / RyuAtelier. If you notice any bugs, please create an issue on this repo.
