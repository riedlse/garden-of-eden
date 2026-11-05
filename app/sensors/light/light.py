import argparse
import logging
import time

import pigpio
from gpiozero import PWMLED
from gpiozero.pins.pigpio import PiGPIOFactory

import config


class GPIOController:
    def __init__(self, pin, pin_factory=None):
        self.pin = pin
        self.factory = pin_factory
        if config.PIGPIO_HOST:
            self.pi = pigpio.pi(config.PIGPIO_HOST, config.PIGPIO_PORT)
        else:
            self.pi = pigpio.pi()

        if not self.pi.connected:
            raise RuntimeError(
                "Failed to connect to pigpiod daemon. Ensure it's running and accessible."
            )

    def set_frequency(self, frequency):
        if self.pi:
            self.pi.set_PWM_frequency(self.pin, frequency)
        else:
            raise RuntimeError("pigpio.pi client is not initialized.")


class Light:
    def __init__(self, pin=config.LIGHT_PIN, frequency=config.LIGHT_FREQUENCY, pin_factory=None):
        # pigpiod is running on port 8888
        # Note: for docker: PiGPIOFactory(host='pigpiod', port=8888)
        self.pin = pin
        self.pin_factory = pin_factory if pin_factory else PiGPIOFactory()
        # Look at the pin *before* gpiozero takes it over: starting PWM through
        # gpiozero writes the pin low and sets duty 0, which is why every service
        # (re)start used to switch the grow light off. Remember the duty pigpiod
        # is already running and put it back once PWM is set up.
        self.gpio = GPIOController(pin, pin_factory)
        previous = self._running_duty()
        self.led = PWMLED(self.pin, pin_factory=self.pin_factory)
        self.set_frequency(frequency)
        if previous:
            logging.info("Preserving light duty_cycle of %.0f%% across restart", previous * 100)
            self.led.value = previous

    def _running_duty(self):
        """Duty (0..1) the pin is already running via pigpiod, or ``None``."""
        try:
            duty = self.gpio.pi.get_PWM_dutycycle(self.pin)
            span = self.gpio.pi.get_PWM_range(self.pin)
        except Exception:
            return None  # pigpio raises when the pin is not running PWM
        if not isinstance(duty, (int, float)) or not isinstance(span, (int, float)) or span <= 0:
            return None
        return max(0.0, min(1.0, duty / span))

    def on(self):
        """
        Turn on lights.
        """
        if self.led.value > 0:
            logging.info("Light already on, skipping")
            return

        logging.info("Turning light on")
        self.led.value = 1

    def off(self):
        """
        Turn off lights.
        """
        logging.info("Turning light off")
        self.led.value = 0

    def set_brightness(self, brightness_percentage):
        """
        Wrapper function around set_duty_cycle. Provides more intuitive function name.

        Args:
        - brightness_percentage (int): A value between 0 (off) and 100 (max brightness).
        """
        self.set_duty_cycle(brightness_percentage)

    def get_brightness(self):
        """
        Wrapper function around get_duty_cycle. Provides more intuitive function name.

        Returns:
        - float: The current duty cycle percentage.
        """
        return self.get_duty_cycle()

    def set_frequency(self, frequency):
        logging.info(f"Setting light frequency to {frequency}")
        self.gpio.set_frequency(frequency)

    def set_duty_cycle(self, duty_cycle_percentage):
        """
        Set the duty cycle percentage, i.e. brightness level.

        Args:
        - duty_cycle_percentage (int): A value between 0 (off) and 100 (full brightness).
        """
        if 0 <= duty_cycle_percentage <= 100:
            # gpiozero's PWMLED uses a 0-1 scale for duty cycle
            duty = duty_cycle_percentage / 100.0
            logging.info(f"Setting light duty_cycle to {duty_cycle_percentage}%")
            self.led.value = duty
        else:
            raise ValueError("Speed must be between 0 and 100")

    def get_duty_cycle(self):
        """
        Get the current duty cycle percentage.

        Returns:
        - float: The current duty cycle percentage.
        """
        duty_cycle = self.led.value * 100
        logging.info(f"Light duty_cycle is {duty_cycle}%")
        return duty_cycle

    def close(self):
        self.led.close()


def ramp_to(light, target, minutes):
    """Gradually move brightness from its current level to ``target`` over
    ``minutes`` (sunrise/sunset). ``target`` of 0 ends with the light off."""
    target = max(0, min(100, int(target)))
    start = light.get_brightness()
    total = max(0, int(minutes)) * 60
    if total <= 0:
        light.set_brightness(target)
        return
    steps = max(1, min(int(total), 60))  # at most ~1 update/sec, capped at 60
    delay = total / steps
    for i in range(1, steps + 1):
        value = start + (target - start) * i / steps
        light.set_brightness(int(round(value)))
        if i < steps:
            time.sleep(delay)
    light.set_brightness(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Control an IoT light.")
    parser.add_argument("--on", action="store_true", help="Turn the light on.")
    parser.add_argument("--off", action="store_true", help="Turn the light off.")
    parser.add_argument(
        "--brightness", type=int, default=None, help="Set the brightness level (0-100)."
    )
    parser.add_argument(
        "--ramp-minutes",
        type=int,
        default=0,
        help="Gradually ramp to --brightness over this many minutes (sunrise/sunset).",
    )

    args = parser.parse_args()

    light = Light()  # pins/frequency from config

    if args.ramp_minutes and args.brightness is not None:
        ramp_to(light, args.brightness, args.ramp_minutes)
    elif args.on:
        light.on()
        if args.brightness is not None:
            light.set_brightness(args.brightness)
    elif args.off:
        light.off()
    elif args.brightness is not None:
        light.on()
        light.set_brightness(args.brightness)
    else:
        logging.info("No action specified. Use --on, --off, or --brightness.")
