"""What the Companion reports in its `state` message. A new sensor is one entry in SENSORS."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import Link
from .entity import CompanionEntity

SCREEN = ["on", "dim", "off"]


def number(field: str) -> Callable[[Link], int | None]:
    """The state's `field` when it is a whole number; a firmware may lack it, or send something newer."""

    def value(link: Link) -> int | None:
        got = link.state.get(field)
        return got if type(got) is int else None

    return value


def screen(link: Link) -> str | None:
    got = link.state.get("screen")
    return got if got in SCREEN else None


@dataclass(frozen=True, kw_only=True)
class CompanionSensorDescription(SensorEntityDescription):
    """`key` is the unique id and the translation key; `value` reads it from the link."""

    value: Callable[[Link], int | str | datetime | None]


SENSORS = (
    CompanionSensorDescription(
        key="rssi",
        value=number("rssi"),
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    CompanionSensorDescription(key="normal", value=number("normal"), state_class=SensorStateClass.MEASUREMENT),
    CompanionSensorDescription(key="urgent", value=number("urgent"), state_class=SensorStateClass.MEASUREMENT),
    CompanionSensorDescription(key="screen", value=screen, device_class=SensorDeviceClass.ENUM, options=SCREEN),
    CompanionSensorDescription(
        key="heap",
        value=number("heap"),
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    CompanionSensorDescription(
        key="last_boot",
        value=lambda link: link.boot,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(CompanionSensor(entry, description) for description in SENSORS)


class CompanionSensor(CompanionEntity, SensorEntity):
    entity_description: CompanionSensorDescription

    def __init__(self, entry: ConfigEntry, description: CompanionSensorDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | str | datetime | None:
        return self.entity_description.value(self.link)
