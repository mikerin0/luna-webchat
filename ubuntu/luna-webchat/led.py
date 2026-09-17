import asyncio

import led_controller
from pi_control import _cancel_lcd_gpu_monitor


def _queue_led_reply(reply: str) -> None:
  if not led_controller.ENABLED:
    return
  task = asyncio.create_task(led_controller.display_reply(reply))
  task.add_done_callback(lambda completed: completed.exception())


def _queue_led_companion(prompt: str, reply: str) -> None:
  if not led_controller.ENABLED:
    return
  task = asyncio.create_task(led_controller.display_companion(prompt, reply))
  task.add_done_callback(lambda completed: completed.exception())


def _queue_led_request(prompt: str) -> bool:
  if not led_controller.ENABLED or not led_controller.requested_label(prompt):
    return False
  _cancel_lcd_gpu_monitor()
  task = asyncio.create_task(led_controller.display_requested(prompt))
  task.add_done_callback(lambda completed: completed.exception())
  return True
