from datetime import datetime, timedelta
import asyncio

# Configuration Constants
MIN_UPDATE_SECONDS = 1
MAX_UPDATE_SECONDS = 60
ADDITIONAL_DELAY_PER_UPDATE = 1

class ThrottledUpdater:
    def __init__(self, hass, update_callback):
        self.hass = hass
        self.update_callback = update_callback
        self.min_update = timedelta(seconds=MIN_UPDATE_SECONDS)
        self.max_update = timedelta(seconds=MAX_UPDATE_SECONDS)
        self.additional_delay_per_update = ADDITIONAL_DELAY_PER_UPDATE
        self.last_update = datetime.min
        self.successive_updates = 0
        self.update_task = None  # Task for managing updates

    def request_update(self):
        """Request an update, managing async logic within Home Assistant's event loop."""
        if self.update_task is None or self.update_task.done():
            self.update_task = self.hass.async_create_task(self.manage_update())

    async def manage_update(self):
        """Manage update requests, applying throttling logic."""
        now = datetime.now()
        time_since_last_update = now - self.last_update

        # Calculate the dynamic update delay
        update_delay = self.calculate_delay(time_since_last_update)

        if update_delay > timedelta(0):
            await asyncio.sleep(update_delay.total_seconds())

        # Reset the successive updates counter if the actual delay was longer than calculated
        if datetime.now() - self.last_update >= self.max_update:
            self.successive_updates = 0
        else:
            self.successive_updates += 1

        await self.update_callback()
        self.last_update = datetime.now()

    def calculate_delay(self, time_since_last_update):
        """Calculate the delay needed before the next update can be performed."""
        update_delay = self.min_update + timedelta(seconds=self.successive_updates * self.additional_delay_per_update)
        update_delay = min(update_delay, self.max_update)
        remaining_delay = update_delay - time_since_last_update
        return max(remaining_delay, timedelta(0))

