# CMakeLists.txt for EV Guardian Zephyr RTOS Firmware
cmake_minimum_required(VERSION 3.20.0)

# Locate the Zephyr installation and setup CMake environment
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ev_guardian_firmware)

# Link the main firmware implementation file
target_sources(app PRIVATE main.c)
