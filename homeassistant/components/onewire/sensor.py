"""Support for 1-Wire environment sensors."""
from glob import glob
import logging
import os
from typing import Any, Dict, Optional

from pi1wire import InvalidCRCException, Pi1Wire, UnsupportResponseException
from pyownet import protocol
import voluptuous as vol

from homeassistant.components.onewire.onewirehub import OneWireHub
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TYPE,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_HUMIDITY,
    DEVICE_CLASS_ILLUMINANCE,
    DEVICE_CLASS_PRESSURE,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_VOLTAGE,
    ELECTRICAL_CURRENT_AMPERE,
    LIGHT_LUX,
    PERCENTAGE,
    PRESSURE_MBAR,
    TEMP_CELSIUS,
    VOLT,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import StateType

from .const import (
    CONF_MOUNT_DIR,
    CONF_NAMES,
    CONF_TYPE_OWFS,
    CONF_TYPE_OWSERVER,
    CONF_TYPE_SYSBUS,
    DEFAULT_OWSERVER_PORT,
    DEFAULT_SYSBUS_MOUNT_DIR,
    DOMAIN,
    PRESSURE_CBAR,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_SENSORS = {
    # Family : { SensorType: owfs path }
    "10": {"temperature": "temperature"},
    "12": {"temperature": "TAI8570/temperature", "pressure": "TAI8570/pressure"},
    "22": {"temperature": "temperature"},
    "26": {
        "temperature": "temperature",
        "humidity": "humidity",
        "humidity_hih3600": "HIH3600/humidity",
        "humidity_hih4000": "HIH4000/humidity",
        "humidity_hih5030": "HIH5030/humidity",
        "humidity_htm1735": "HTM1735/humidity",
        "pressure": "B1-R1-A/pressure",
        "illuminance": "S3-R1-A/illuminance",
        "voltage_VAD": "VAD",
        "voltage_VDD": "VDD",
        "current": "IAD",
    },
    "28": {"temperature": "temperature"},
    "3B": {"temperature": "temperature"},
    "42": {"temperature": "temperature"},
    "1D": {"counter_a": "counter.A", "counter_b": "counter.B"},
    "EF": {"HobbyBoard": "special"},
}

DEVICE_SUPPORT_SYSBUS = ["10", "22", "28", "3B", "42"]

# EF sensors are usually hobbyboards specialized sensors.
# These can only be read by OWFS.  Currently this driver only supports them
# via owserver (network protocol)

HOBBYBOARD_EF = {
    "HobbyBoards_EF": {
        "humidity": "humidity/humidity_corrected",
        "humidity_raw": "humidity/humidity_raw",
        "temperature": "humidity/temperature",
    },
    "HB_MOISTURE_METER": {
        "moisture_0": "moisture/sensor.0",
        "moisture_1": "moisture/sensor.1",
        "moisture_2": "moisture/sensor.2",
        "moisture_3": "moisture/sensor.3",
    },
}

SENSOR_TYPES = {
    # SensorType: [ Measured unit, Unit, DeviceClass ]
    "temperature": ["temperature", TEMP_CELSIUS, DEVICE_CLASS_TEMPERATURE],
    "humidity": ["humidity", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "humidity_hih3600": ["humidity", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "humidity_hih4000": ["humidity", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "humidity_hih5030": ["humidity", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "humidity_htm1735": ["humidity", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "humidity_raw": ["humidity", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "pressure": ["pressure", PRESSURE_MBAR, DEVICE_CLASS_PRESSURE],
    "illuminance": ["illuminance", LIGHT_LUX, DEVICE_CLASS_ILLUMINANCE],
    "wetness_0": ["wetness", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "wetness_1": ["wetness", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "wetness_2": ["wetness", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "wetness_3": ["wetness", PERCENTAGE, DEVICE_CLASS_HUMIDITY],
    "moisture_0": ["moisture", PRESSURE_CBAR, DEVICE_CLASS_PRESSURE],
    "moisture_1": ["moisture", PRESSURE_CBAR, DEVICE_CLASS_PRESSURE],
    "moisture_2": ["moisture", PRESSURE_CBAR, DEVICE_CLASS_PRESSURE],
    "moisture_3": ["moisture", PRESSURE_CBAR, DEVICE_CLASS_PRESSURE],
    "counter_a": ["counter", "count", None],
    "counter_b": ["counter", "count", None],
    "HobbyBoard": ["none", "none", None],
    "voltage": ["voltage", VOLT, DEVICE_CLASS_VOLTAGE],
    "voltage_VAD": ["voltage", VOLT, DEVICE_CLASS_VOLTAGE],
    "voltage_VDD": ["voltage", VOLT, DEVICE_CLASS_VOLTAGE],
    "current": ["current", ELECTRICAL_CURRENT_AMPERE, DEVICE_CLASS_CURRENT],
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAMES): {cv.string: cv.string},
        vol.Optional(CONF_MOUNT_DIR, default=DEFAULT_SYSBUS_MOUNT_DIR): cv.string,
        vol.Optional(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_OWSERVER_PORT): cv.port,
    }
)


def hb_info_from_type(dev_type="std"):
    """Return the proper info array for the device type."""
    if "std" in dev_type:
        return DEVICE_SENSORS
    if "HobbyBoard" in dev_type:
        return HOBBYBOARD_EF


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Old way of setting up 1-Wire platform."""
    if config.get(CONF_HOST):
        config[CONF_TYPE] = CONF_TYPE_OWSERVER
    elif config[CONF_MOUNT_DIR] == DEFAULT_SYSBUS_MOUNT_DIR:
        config[CONF_TYPE] = CONF_TYPE_SYSBUS
    else:  # pragma: no cover
        # This part of the implementation does not conform to policy regarding 3rd-party libraries, and will not longer be updated.
        # https://developers.home-assistant.io/docs/creating_platform_code_review/#5-communication-with-devicesservices
        config[CONF_TYPE] = CONF_TYPE_OWFS

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=config
        )
    )


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up 1-Wire platform."""
    onewirehub = hass.data[DOMAIN][config_entry.unique_id]
    entities = await hass.async_add_executor_job(
        get_entities, onewirehub, config_entry.data
    )
    async_add_entities(entities, True)


def get_entities(onewirehub: OneWireHub, config):
    """Get a list of entities."""
    entities = []
    device_names = {}
    if CONF_NAMES in config:
        if isinstance(config[CONF_NAMES], dict):
            device_names = config[CONF_NAMES]

    conf_type = config[CONF_TYPE]
    # We have an owserver on a remote(or local) host/port
    if conf_type == CONF_TYPE_OWSERVER:
        owhost = config[CONF_HOST]
        owport = config[CONF_PORT]

        try:
            devices = onewirehub.owproxy.dir()
        except protocol.OwnetError as exc:
            _LOGGER.error(
                "Failed to list devices on %s:%d, got: %s", owhost, owport, exc
            )
            return entities
        for device in devices:
            _LOGGER.debug("Found device: %s", device)
            family = onewirehub.owproxy.read(f"{device}family").decode()
            device_type = onewirehub.owproxy.read(f"{device}type").decode()
            sensor_id = os.path.split(os.path.split(device)[0])[1]
            dev_type = "std"
            if "EF" in family:
                dev_type = "HobbyBoard"
                family = device_type

            if family not in hb_info_from_type(dev_type):
                _LOGGER.warning(
                    "Ignoring unknown family (%s) of sensor found for device: %s",
                    family,
                    device,
                )
                continue
            device_info = {
                "identifiers": {(DOMAIN, sensor_id)},
                "manufacturer": "Maxim Integrated",
                "model": device_type,
                "name": sensor_id,
            }
            for sensor_key, sensor_value in hb_info_from_type(dev_type)[family].items():
                if "moisture" in sensor_key:
                    s_id = sensor_key.split("_")[1]
                    is_leaf = int(
                        onewirehub.owproxy.read(
                            f"{device}moisture/is_leaf.{s_id}"
                        ).decode()
                    )
                    if is_leaf:
                        sensor_key = f"wetness_{s_id}"
                device_file = os.path.join(os.path.split(device)[0], sensor_value)
                entities.append(
                    OneWireProxy(
                        device_names.get(sensor_id, sensor_id),
                        device_file,
                        sensor_key,
                        device_info,
                        onewirehub.owproxy,
                    )
                )

    # We have a raw GPIO ow sensor on a Pi
    elif conf_type == CONF_TYPE_SYSBUS:
        base_dir = config[CONF_MOUNT_DIR]
        _LOGGER.debug("Initializing using SysBus %s", base_dir)
        for p1sensor in Pi1Wire(base_dir).find_all_sensors():
            family = p1sensor.mac_address[:2]
            sensor_id = f"{family}-{p1sensor.mac_address[2:]}"
            if family not in DEVICE_SUPPORT_SYSBUS:
                _LOGGER.warning(
                    "Ignoring unknown family (%s) of sensor found for device: %s",
                    family,
                    sensor_id,
                )
                continue

            device_info = {
                "identifiers": {(DOMAIN, sensor_id)},
                "manufacturer": "Maxim Integrated",
                "model": family,
                "name": sensor_id,
            }
            device_file = f"/sys/bus/w1/devices/{sensor_id}/w1_slave"
            entities.append(
                OneWireDirect(
                    device_names.get(sensor_id, sensor_id),
                    device_file,
                    "temperature",
                    device_info,
                    p1sensor,
                )
            )
        if not entities:
            _LOGGER.error(
                "No onewire sensor found. Check if dtoverlay=w1-gpio "
                "is in your /boot/config.txt. "
                "Check the mount_dir parameter if it's defined"
            )

    # We have an owfs mounted
    else:  # pragma: no cover
        # This part of the implementation does not conform to policy regarding 3rd-party libraries, and will not longer be updated.
        # https://developers.home-assistant.io/docs/creating_platform_code_review/#5-communication-with-devicesservices
        base_dir = config[CONF_MOUNT_DIR]
        _LOGGER.debug("Initializing using OWFS %s", base_dir)
        _LOGGER.warning(
            "The OWFS implementation of 1-Wire sensors is deprecated, "
            "and should be migrated to OWServer (on localhost:4304). "
            "If migration to OWServer is not feasible on your installation, "
            "please raise an issue at https://github.com/home-assistant/core/issues/new"
            "?title=Unable%20to%20migrate%20onewire%20from%20OWFS%20to%20OWServer",
        )
        for family_file_path in glob(os.path.join(base_dir, "*", "family")):
            with open(family_file_path) as family_file:
                family = family_file.read()
            if "EF" in family:
                continue
            if family in DEVICE_SENSORS:
                for sensor_key, sensor_value in DEVICE_SENSORS[family].items():
                    sensor_id = os.path.split(os.path.split(family_file_path)[0])[1]
                    device_file = os.path.join(
                        os.path.split(family_file_path)[0], sensor_value
                    )
                    entities.append(
                        OneWireOWFS(
                            device_names.get(sensor_id, sensor_id),
                            device_file,
                            sensor_key,
                        )
                    )

    return entities


class OneWire(Entity):
    """Implementation of a 1-Wire sensor."""

    def __init__(self, name, device_file, sensor_type, device_info=None):
        """Initialize the sensor."""
        self._name = f"{name} {sensor_type.capitalize()}"
        self._device_file = device_file
        self._device_class = SENSOR_TYPES[sensor_type][2]
        self._unit_of_measurement = SENSOR_TYPES[sensor_type][1]
        self._device_info = device_info
        self._state = None
        self._value_raw = None

    @property
    def name(self) -> Optional[str]:
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self) -> StateType:
        """Return the state of the sensor."""
        if "count" in self._unit_of_measurement:
            return int(self._state)
        return self._state

    @property
    def device_class(self) -> Optional[str]:
        """Return the class of this device."""
        return self._device_class

    @property
    def unit_of_measurement(self) -> Optional[str]:
        """Return the unit the value is expressed in."""
        return self._unit_of_measurement

    @property
    def device_state_attributes(self) -> Optional[Dict[str, Any]]:
        """Return the state attributes of the sensor."""
        return {"device_file": self._device_file, "raw_value": self._value_raw}

    @property
    def unique_id(self) -> Optional[str]:
        """Return a unique ID."""
        return self._device_file

    @property
    def device_info(self) -> Optional[Dict[str, Any]]:
        """Return device specific attributes."""
        return self._device_info


class OneWireProxy(OneWire):
    """Implementation of a 1-Wire sensor through owserver."""

    def __init__(self, name, device_file, sensor_type, device_info, owproxy):
        """Initialize the sensor."""
        super().__init__(name, device_file, sensor_type, device_info)
        self._owproxy = owproxy

    def _read_value_ownet(self):
        """Read a value from the owserver."""
        return self._owproxy.read(self._device_file).decode().lstrip()

    def update(self):
        """Get the latest data from the device."""
        value = None
        value_read = False
        try:
            value_read = self._read_value_ownet()
        except protocol.Error as exc:
            _LOGGER.error("Owserver failure in read(), got: %s", exc)
        if value_read:
            value = round(float(value_read), 1)
            self._value_raw = float(value_read)

        self._state = value


class OneWireDirect(OneWire):
    """Implementation of a 1-Wire sensor directly connected to RPI GPIO."""

    def __init__(self, name, device_file, sensor_type, device_info, owsensor):
        """Initialize the sensor."""
        super().__init__(name, device_file, sensor_type, device_info)
        self._owsensor = owsensor

    def update(self):
        """Get the latest data from the device."""
        value = None
        try:
            self._value_raw = self._owsensor.get_temperature()
            value = round(float(self._value_raw), 1)
        except (
            FileNotFoundError,
            InvalidCRCException,
            UnsupportResponseException,
        ) as ex:
            _LOGGER.warning("Cannot read from sensor %s: %s", self._device_file, ex)
        self._state = value


class OneWireOWFS(OneWire):  # pragma: no cover
    """Implementation of a 1-Wire sensor through owfs.

    This part of the implementation does not conform to policy regarding 3rd-party libraries, and will not longer be updated.
    https://developers.home-assistant.io/docs/creating_platform_code_review/#5-communication-with-devicesservices
    """

    def _read_value_raw(self):
        """Read the value as it is returned by the sensor."""
        with open(self._device_file) as ds_device_file:
            lines = ds_device_file.readlines()
        return lines

    def update(self):
        """Get the latest data from the device."""
        value = None
        try:
            value_read = self._read_value_raw()
            if len(value_read) == 1:
                value = round(float(value_read[0]), 1)
                self._value_raw = float(value_read[0])
        except ValueError:
            _LOGGER.warning("Invalid value read from %s", self._device_file)
        except FileNotFoundError:
            _LOGGER.warning("Cannot read from sensor: %s", self._device_file)

        self._state = value
