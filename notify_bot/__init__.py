from notify_bot.main import run_bot  # noqa: F403, F401

import importlib
import notify_bot.main

# Reload the module before calling run_bot function
importlib.reload(notify_bot.main)
notify_bot.main.run_bot()

all = [run_bot]  # noqa: F405
