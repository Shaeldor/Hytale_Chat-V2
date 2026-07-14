# Hytale Chat Tunnel

Welcome to the **Hytale Chat Tunnel** project! 

This repository houses a custom proximity voice and text chat client overlay built specifically for Hytale (and similar environments) utilizing QUIC network tunneling.

## 💻 Operating System Versions

Because the architecture for hooking, memory scanning, and window overlays differs drastically between operating systems, we maintain two entirely separate client versions side-by-side in this repository.

Please navigate to the folder corresponding to your Operating System for specific installation and usage instructions:

### [📁 Windows Client (V6.1)](./Windows)
The Windows client is built using Python `ctypes` and specifically hooks into Windows API functions (`kernel32`, `user32`) for memory reading, global hotkeys, and borderless transparent window overlays. 

- **Supports:** Windows 10 / Windows 11
- **Features:** GIF support, dynamic emoticon conversion, Quiche networking, borderless transparent overlay, memory-based player name scanning.

### [📁 Linux Client](./Linux)
The Linux client utilizes native Linux system calls, memory I/O via `/proc/<pid>/mem`, and custom `focus-toggle.sh` scripts for interacting with window managers like Hyprland/Wayland.

- **Supports:** Linux (Wayland/Hyprland)
- **Features:** Advanced packet injection, QUIC probe networking, Wayland-compatible overlay, process memory scanning.

## 🚀 Getting Started

To get started, clone this repository and navigate into the respective folder for your operating system:

```bash
git clone https://github.com/Shaeldor/Hytale_Chat-V2.git
cd Hytale_Chat-V2

# For Windows:
cd Windows

# For Linux:
cd Linux
```

Read the `README.md` file inside your chosen folder for specific build, installation, and run instructions.

## 🤝 Contributing

When contributing to this project, please ensure that you are making changes inside the appropriate OS folder (`Windows/` or `Linux/`). 

Because the codebases are isolated, updates to one operating system will not cause merge conflicts with the other!
