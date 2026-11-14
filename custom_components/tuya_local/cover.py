"""
Setup for different kinds of Tuya cover devices
"""

import logging

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity
from .helpers.config import async_tuya_setup_platform
from .helpers.device_config import TuyaEntityConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = {**config_entry.data, **config_entry.options}
    await async_tuya_setup_platform(
        hass,
        async_add_entities,
        config,
        "cover",
        TuyaLocalCover,
    )


class TuyaLocalCover(TuyaLocalEntity, CoverEntity):
    """Representation of a Tuya Cover Entity."""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the cover device.
        Args:
          device (TuyaLocalDevice): The device API instance
          config (TuyaEntityConfig): The entity config
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._position_dp = dps_map.pop("position", None)
        self._currentpos_dp = dps_map.pop("current_position", None)
        self._tiltpos_dp = dps_map.pop("tilt_position", None)
        self._control_dp = dps_map.pop("control", None)
        self._action_dp = dps_map.pop("action", None)
        self._open_dp = dps_map.pop("open", None)
        self._init_end(dps_map)

        self._use_control_for_endpoints = (
            config.use_control_for_endpoints
            and self._control_dp is not None
            and self._position_dp is not None
            and self._currentpos_dp is not None
        )
        self._motion_command = None
        self._last_control = None
        self._last_target = None

        self._support_flags = CoverEntityFeature(0)
        if self._position_dp:
            self._support_flags |= CoverEntityFeature.SET_POSITION
        if self._control_dp:
            if "stop" in self._control_dp.values(self._device):
                self._support_flags |= CoverEntityFeature.STOP
            if "open" in self._control_dp.values(self._device):
                self._support_flags |= CoverEntityFeature.OPEN
            if "close" in self._control_dp.values(self._device):
                self._support_flags |= CoverEntityFeature.CLOSE
        if self._tiltpos_dp:
            self._support_flags |= CoverEntityFeature.SET_TILT_POSITION

        # Open/close/stop tilt not yet supported, as no test devices known

    @property
    def device_class(self):
        """Return the class of ths device"""
        dclass = self._config.device_class
        try:
            return CoverDeviceClass(dclass)
        except ValueError:
            if dclass:
                _LOGGER.warning(
                    "%s/%s: Unrecognised cover device class of %s ignored",
                    self._config._device.config,
                    self.name or "cover",
                    dclass,
                )

    @property
    def supported_features(self):
        """Inform HA of the supported features."""
        return self._support_flags

    def _state_to_percent(self, state):
        """Convert a state to percent open"""
        if state == "opened":
            return 100
        elif state == "closed":
            return 0
        else:
            return 50

    @property
    def current_cover_position(self):
        """Return current position of cover."""
        if self._currentpos_dp:
            pos = self._currentpos_dp.get_value(self._device)
            if pos is not None:
                return pos

        if self._use_control_for_endpoints:
            # A target is not a measurement, particularly after a Stop.
            return None

        if self._open_dp:
            state = self._open_dp.get_value(self._device)
            if state is not None:
                return 100 if state else 0

        if self._action_dp:
            state = self._action_dp.get_value(self._device)
            if state is not None:
                return self._state_to_percent(state)

        if self._position_dp:
            pos = self._position_dp.get_value(self._device)
            return pos

    @property
    def current_cover_tilt_position(self):
        """Return current tilt position of cover."""
        if self._tiltpos_dp:
            r = self._tiltpos_dp.range(self._device)
            val = self._tiltpos_dp.get_value(self._device)
            if r and val is not None:
                return ranged_value_to_percentage(r, val)
            return val

    def on_receive(self, dps, full_poll):
        """Identify the latest command from the motor's DP updates."""
        if not self._use_control_for_endpoints:
            return

        control_id = self._control_dp.id
        target_id = self._position_dp.id
        if control_id in dps:
            control = dps[control_id]
            control_changed = (
                self._last_control is not None and control != self._last_control
            )
            if control_changed or target_id not in dps:
                self._motion_command = self._control_dp.get_value(self._device)
            self._last_control = control

        if target_id in dps:
            target = dps[target_id]
            target_changed = (
                self._last_target is not None and target != self._last_target
            )
            if control_id not in dps or (target_changed and not control_changed):
                self._motion_command = "position"
            self._last_target = target

    def _state_from_motion_command(self):
        """Use the current position and latest command for movement status."""
        position = self.current_cover_position
        if position is None:
            return None

        if self._motion_command == "position":
            target = self._position_dp.get_value(self._device)
        else:
            target = {"open": 100, "close": 0}.get(self._motion_command)

        if target is not None and abs(target - position) > 2:
            return "opening" if target > position else "closing"
        return "closed" if position == 0 else "opened"

    @property
    def _current_state(self):
        """Return the current state of the cover if it can be determined,
        or None if it is inconclusive.
        """
        if self._use_control_for_endpoints:
            return self._state_from_motion_command()

        if self._action_dp:
            action = self._action_dp.get_value(self._device)
            if action in ["opening", "closing", "opened", "closed"]:
                return action

        pos = self.current_cover_position
        if pos is None:
            return None
        if pos < 5:
            return "closed"
        elif pos > 95:
            return "opened"

        if (
            self._currentpos_dp
            and self._currentpos_dp.get_value(self._device) is not None
            and self._position_dp
        ):
            setpos = self._position_dp.get_value(self._device)
            if setpos is not None and abs(setpos - pos) <= 2:
                # if the current position is around the set position,
                # which is not closed, then we want is_closed to return
                # false, so HA gets the full state from position.
                return "opened"

            if self._control_dp:
                cmd = self._control_dp.get_value(self._device)
                if cmd == "open":
                    return "opening"
                elif cmd == "close":
                    return "closing"

    @property
    def is_opening(self):
        """Return if the cover is opening or not."""
        state = self._current_state
        if state is None:
            # If we return false, and is_closing and is_opening are also false,
            # HA assumes open.  If we don't know, return None.
            return None
        else:
            return state == "opening"

    @property
    def is_closing(self):
        """Return if the cover is closing or not."""
        state = self._current_state
        if state is None:
            # If we return false, and is_closing and is_opening are also false,
            # HA assumes open.  If we don't know, return None.
            return None
        else:
            return state == "closing"

    @property
    def is_closed(self):
        """Return if the cover is closed or not, if it can be determined."""
        state = self._current_state
        if state is None:
            # If we return false, and is_closing and is_opening are also false,
            # HA assumes open.  If we don't know, return None.
            return None
        else:
            return state == "closed"

    async def async_open_cover(self, **kwargs):
        """Open the cover."""
        if self._control_dp and "open" in self._control_dp.values(self._device):
            _LOGGER.info("%s opening", self._config.config_id)
            await self._control_dp.async_set_value(self._device, "open")
        elif self._position_dp:
            pos = 100
            _LOGGER.info("%s opening to 100%%", self._config.config_id)
            await self._position_dp.async_set_value(self._device, pos)
        else:
            raise NotImplementedError()

    async def async_close_cover(self, **kwargs):
        """Close the cover."""
        if self._control_dp and "close" in self._control_dp.values(self._device):
            _LOGGER.info("%s closing", self._config.config_id)
            await self._control_dp.async_set_value(self._device, "close")
        elif self._position_dp:
            pos = 0
            _LOGGER.info("%s closing to 0%%", self._config.config_id)
            await self._position_dp.async_set_value(self._device, pos)
        else:
            raise NotImplementedError()

    async def async_set_cover_position(self, position, **kwargs):
        """Set the cover to a specific position."""
        if position is None:
            raise AttributeError()
        if self._use_control_for_endpoints:
            if position == 0 and self.supported_features & CoverEntityFeature.CLOSE:
                await self.async_close_cover()
                return
            if position == 100 and self.supported_features & CoverEntityFeature.OPEN:
                await self.async_open_cover()
                return
        if self._position_dp:
            _LOGGER.info(
                "%s setting position to %d%%", self._config.config_id, position
            )
            await self._position_dp.async_set_value(self._device, position)
        else:
            raise NotImplementedError()

    async def async_set_cover_tilt_position(self, tilt_position, **kwargs):
        """Set the cover tilt position."""
        if self._tiltpos_dp:
            # If there is a fixed list of values, snap to the closest one
            if self._tiltpos_dp.values(self._device):
                tilt_position = min(
                    self._tiltpos_dp.values(self._device),
                    key=lambda x: abs(x - tilt_position),
                )
            elif self._tiltpos_dp.range(self._device):
                r = self._tiltpos_dp.range(self._device)
                tilt_position = percentage_to_ranged_value(r, tilt_position)

            _LOGGER.info(
                "%s setting tilt position to %d%%",
                self._config.config_id,
                tilt_position,
            )
            await self._tiltpos_dp.async_set_value(self._device, tilt_position)
        else:
            raise NotImplementedError

    async def async_stop_cover(self, **kwargs):
        """Stop the cover."""
        if self._control_dp and "stop" in self._control_dp.values(self._device):
            _LOGGER.info("%s stopping", self._config.config_id)
            await self._control_dp.async_set_value(self._device, "stop")
        else:
            raise NotImplementedError()
