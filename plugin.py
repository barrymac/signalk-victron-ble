import argparse
import asyncio
import datetime
import dataclasses
import json
import logging
import os
import sys
from typing import Any, Callable, TypeVar, Union

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from victron_ble.devices import (
    AuxMode,
    BatteryMonitorData,
    BatterySenseData,
    DcDcConverterData,
    DeviceData,
    InverterData,
    LynxSmartBMSData,
    OrionXSData,
    SmartLithiumData,
    SolarChargerData,
    VEBusData,
)
from victron_ble.exceptions import AdvertisementKeyMissingError, UnknownDeviceError
from victron_ble.scanner import Scanner

T = TypeVar('T', bound=DeviceData)

logger = logging.getLogger("signalk-victron-ble")

logger.debug("victron plugin starting up")

# 3.9 compatible TypeAliases
SignalKDelta = dict[str, list[dict[str, Any]]]
SignalKDeltaValues = list[dict[str, Union[int, float, str, None]]]


@dataclasses.dataclass
class ConfiguredDevice:
    id: str
    mac: str
    advertisement_key: str
    secondary_battery: Union[str, None]
    link_to_engine: bool = False
    engine_id: str = "mainEngine"


class SignalKScanner(Scanner):
    _devices: dict[str, ConfiguredDevice]
    discovered_devices: set[str]

    def __init__(self, devices: dict[str, ConfiguredDevice]) -> None:
        # Add debug logging for parent class inspection
        logger.debug(f"Parent __init__ signature: {inspect.signature(super().__init__)}")
        try:
            super().__init__()
        except TypeError as e:
            logger.debug(f"Parent __init__ args required: {e}")
            raise
        self._devices = devices
        self.discovered_devices = set()

    def load_key(self, address: str) -> str:
        try:
            return self._devices[address].advertisement_key
        except KeyError:
            raise AdvertisementKeyMissingError(f"No key available for {address}")

    def callback(self, bl_device: BLEDevice, raw_data: bytes) -> None:
        logger.debug(f"Device discovered: {bl_device}")
        self.discovered_devices.add(bl_device.address.lower())
        
        rssi = getattr(bl_device, "rssi", None)
        if rssi is None:
            logger.debug(f"No RSSI for {bl_device.address.lower()}")
            return
        
        logger.debug(
            f"Received {len(raw_data)}B packet from {bl_device.address.lower()} "
            f"(RSSI: {rssi}) @ {datetime.datetime.now().isoformat()}: "
            f"Payload={raw_data.hex()}"
        )
        
        try:
            device = self.get_device(bl_device, raw_data)
        except AdvertisementKeyMissingError:
            return
        except UnknownDeviceError as e:
            logger.error(e)
            return
        data = device.parse(raw_data)
        configured_device = self._devices[bl_device.address.lower()]
        id_ = configured_device.id
        logger.debug(f"Processing device: ID={id_} MAC={bl_device.address.lower()}")
        transformers: dict[
            type[DeviceData],
            Callable[[BLEDevice, ConfiguredDevice, T, str], SignalKDeltaValues],
        ] = {
            BatteryMonitorData: self.transform_battery_data,
            BatterySenseData: self.transform_battery_sense_data,
            DcDcConverterData: self.transform_dcdc_converter_data,
            InverterData: self.transform_inverter_data,
            LynxSmartBMSData: self.transform_lynx_smart_bms_data,
            OrionXSData: self.transform_orion_xs_data,
            SmartLithiumData: self.transform_smart_lithium_data,
            SolarChargerData: self.transform_solar_charger_data,
            VEBusData: self.transform_ve_bus_data,
        }
        for data_type, transformer in transformers.items():
            if isinstance(data, data_type):
                values = transformer(bl_device, configured_device, data, id_)
                delta = self.prepare_signalk_delta(bl_device, values)
                logger.debug("Generated SignalK delta: %s", json.dumps(delta))
                print(json.dumps(delta))
                sys.stdout.flush()
                return
        else:
            logger.warning("Unknown device type %s from %s", type(device).__name__, bl_device.address.lower())

    def prepare_signalk_delta(
        self, bl_device: BLEDevice, values: SignalKDeltaValues
    ) -> SignalKDelta:
        # Get the configured device for the MAC address
        configured_device = self._devices[bl_device.address.lower()]
        id_ = configured_device.id
        
        # Add device name to deltas
        values.append({
            "path": f"electrical.devices.{id_}.deviceName",
            "value": bl_device.name
        })
        
        return {
            "updates": [
                {
                    "source": {
                        "label": "Victron",
                        "type": "Bluetooth",
                        "src": bl_device.address,
                    },
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                    "values": values,
                }
            ]
        }

    def transform_battery_sense_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: BatterySenseData,
        id_: str,
    ) -> SignalKDeltaValues:
        return [
            {
                "path": f"electrical.batteries.{id_}.voltage",
                "value": data.get_voltage(),
            },
            {
                "path": f"electrical.batteries.{id_}.temperature",
                "value": data.get_temperature() + 273.15,
            },
        ]

    def transform_battery_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: BatteryMonitorData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.deviceMetadata.{id_}.name",
                "value": bl_device.name
            },
            {
                "path": f"electrical.batteries.{id_}.voltage",
                "value": data.get_voltage(),
            },
            {
                "path": f"electrical.batteries.{id_}.current",
                "value": data.get_current(),
            },
            {
                "path": f"electrical.batteries.{id_}.power",
                "value": data.get_voltage() * data.get_current(),
            },
            {
                "path": f"electrical.batteries.{id_}.capacity.stateOfCharge",
                "value": data.get_soc() / 100,
            },
            {
                "path": f"electrical.batteries.{id_}.capacity.dischargeSinceFull",
                "value": data.get_consumed_ah() * 3600,
            },
        ]
        if remaining_mins := data.get_remaining_mins():
            values.append(
                {
                    "path": f"electrical.batteries.{id_}.capacity.timeRemaining",
                    "value": remaining_mins * 60,
                }
            )

        if data.get_aux_mode() == AuxMode.STARTER_VOLTAGE:
            if cfg_device.secondary_battery:
                values.append(
                    {
                        "path": f"electrical.batteries.{cfg_device.secondary_battery}.voltage",
                        "value": data.get_starter_voltage(),
                    }
                )
        elif data.get_aux_mode() == AuxMode.TEMPERATURE:
            if temperature := data.get_temperature():
                values.append(
                    {
                        "path": f"electrical.batteries.{id_}.temperature",
                        "value": temperature + 273.15,
                    }
                )

        return values

    def transform_dcdc_converter_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: DcDcConverterData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.converters.{id_}.chargingMode",
                "value": data.get_charge_state().name.lower(),
            },
            {
                "path": f"electrical.converters.{id_}.chargerError",
                "value": data.get_charger_error().name.lower(),
            },
            {
                "path": f"electrical.converters.{id_}.input.voltage",
                "value": data.get_input_voltage(),
            },
            {
                "path": f"electrical.converters.{id_}.output.voltage",
                "value": data.get_output_voltage(),
            },
        ]
        if off_reason := data.get_off_reason().name:
            values.append(
                {
                    "path": f"electrical.converters.{id_}.chargerOffReason",
                    "value": off_reason.lower(),
                }
            )

        if cfg_device.link_to_engine:
            charge_state = getattr(data.get_charge_state(), 'name', 'unknown').lower()
            charger_error = getattr(data.get_charger_error(), 'name', 'unknown').lower()
                
            # Map to propulsion.*.state.value schema enum
            # Simplified state mapping without unusable
            if charge_state in {'bulk', 'absorption', 'float', 'storage', 'equalize'}:
                engine_state = 'started'
            else:
                engine_state = 'stopped'  # Fallback for all other cases
            
            # Optional: Log error states as warnings
            if charger_error != 'no_error':
                logger.warning(f"Charger error detected: {charger_error}")
                
            values.append({
                "path": f"propulsion.{cfg_device.engine_id}.state.value",
                "value": engine_state
            })
            logger.debug(
                f"Charge: {charge_state}, Error: {charger_error} → {engine_state}"
            )
            
        return values

    def transform_inverter_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: InverterData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.inverters.{id_}.inverterMode",
                "value": data.get_device_state().name.lower(),
            },
            {
                "path": f"electrical.inverters.{id_}.dc.voltage",
                "value": data.get_battery_voltage(),
            },
            {
                "path": f"electrical.inverters.{id_}.ac.apparentPower",
                "value": data.get_ac_apparent_power(),
            },
            {
                "path": f"electrical.inverters.{id_}.ac.lineNeutralVoltage",
                "value": data.get_ac_voltage(),
            },
            {
                "path": f"electrical.inverters.{id_}.ac.current",
                "value": data.get_ac_current(),
            },
        ]
        return values

    def transform_lynx_smart_bms_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: LynxSmartBMSData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.batteries.{id_}.voltage",
                "value": data.get_voltage(),
            },
            {
                "path": f"electrical.batteries.{id_}.current",
                "value": data.get_current(),
            },
            {
                "path": f"electrical.batteries.{id_}.power",
                "value": data.get_voltage() * data.get_current(),
            },
        ]
        if temperature := data.get_battery_temperature():
            values.append(
                {
                    "path": f"electrical.batteries.{id_}.temperature",
                    "value": temperature + 273.15,
                }
            )
        if soc := data.get_soc():
            values.append(
                {
                    "path": f"electrical.batteries.{id_}.capacity.stateOfCharge",
                    "value": soc / 100,
                }
            )
        if consumed_ah := data.get_consumed_ah():
            values.append(
                {
                    "path": f"electrical.batteries.{id_}.capacity.dischargeSinceFull",
                    "value": consumed_ah * 3600,
                }
            )
        if remaining_mins := data.get_remaining_mins():
            values.append(
                {
                    "path": f"electrical.batteries.{id_}.capacity.timeRemaining",
                    "value": remaining_mins * 60,
                }
            )
        return values

    def transform_orion_xs_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: OrionXSData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.converters.{id_}.chargingMode",
                "value": data.get_charge_state().name.lower(),
            },
            {
                "path": f"electrical.converters.{id_}.chargerError",
                "value": data.get_charger_error().name.lower(),
            },
            {
                "path": f"electrical.converters.{id_}.input.voltage",
                "value": data.get_input_voltage(),
            },
            {
                "path": f"electrical.converters.{id_}.input.current",
                "value": data.get_input_current(),
            },
            {
                "path": f"electrical.converters.{id_}.output.voltage",
                "value": data.get_output_voltage(),
            },
            {
                "path": f"electrical.converters.{id_}.output.current",
                "value": data.get_output_current(),
            },
        ]
        if off_reason := data.get_off_reason().name:
            values.append(
                {
                    "path": f"electrical.converters.{id_}.chargerOffReason",
                    "value": off_reason.lower(),
                }
            )
        return values

    def transform_smart_lithium_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: SmartLithiumData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.batteries.{id_}.voltage",
                "value": data.get_battery_voltage(),
            },
        ]
        if temperature := data.get_battery_temperature():
            values.append(
                {
                    "path": f"electrical.batteries.{id_}.temperature",
                    "value": temperature + 273.15,
                }
            )
        return values

    def transform_solar_charger_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: SolarChargerData,
        id_: str,
    ) -> SignalKDeltaValues:
        return [
            {
                "path": f"electrical.solar.{id_}.voltage",
                "value": data.get_battery_voltage(),
            },
            {
                "path": f"electrical.solar.{id_}.current",
                "value": data.get_battery_charging_current(),
            },
            {
                "path": f"electrical.solar.{id_}.chargingMode",
                "value": data.get_charge_state().name.lower(),
            },
            {
                "path": f"electrical.solar.{id_}.panelPower",
                "value": data.get_solar_power(),
            },
            {
                "path": f"electrical.solar.{id_}.loadCurrent",
                "value": data.get_external_device_load(),
            },
            {
                "path": f"electrical.solar.{id_}.yieldToday",
                "value": data.get_yield_today() * 3600,
            },
        ]

    def transform_ve_bus_data(
        self,
        bl_device: BLEDevice,
        cfg_device: ConfiguredDevice,
        data: VEBusData,
        id_: str,
    ) -> SignalKDeltaValues:
        values: SignalKDeltaValues = [
            {
                "path": f"electrical.inverters.{id_}.inverterMode",
                "value": data.get_device_state().name.lower(),
            },
            {
                "path": f"electrical.inverters.{id_}.dc.voltage",
                "value": data.get_battery_voltage(),
            },
            {
                "path": f"electrical.inverters.{id_}.dc.current",
                "value": data.get_battery_current(),
            },
            {
                "path": f"electrical.inverters.{id_}.ac.apparentPower",
                "value": data.get_ac_out_power(),
            },
        ]
        if temperature := data.get_battery_temperature():
            values.append(
                {
                    "path": f"electrical.inverters.{id_}.dc.temperature",
                    "value": temperature + 273.15,
                }
            )
        return values


async def monitor(devices: dict[str, ConfiguredDevice], adapter: str) -> None:
    os.environ["BLUETOOTH_DEVICE"] = adapter
    logger.info(f"Starting Victron BLE monitor on adapter {adapter}")
    
    max_retry_interval = 300  # 5 minutes maximum backoff
    retry_count = 0
    
    while True:
        scanner = None
        try:
            # Reset scanner with fresh instance each retry
            scanner = SignalKScanner(devices)
            logger.debug(f"Scan attempt #{retry_count+1} on {adapter}")
            
            # Scan with extended timeout for device discovery
            await scanner.start()
            
            # Check for core devices missing from discovery
            missing_devices = [
                d.mac for d in devices.values() 
                if d.mac not in scanner.discovered_devices
            ]
            if missing_devices and retry_count < 3:
                logger.warning(f"Missing initial devices: {missing_devices}")
                retry_count += 1
                raise RuntimeError("Initial device discovery incomplete")

            # Allow event loop to process while scanner runs
            await asyncio.Event().wait()
            
        except (RuntimeError, Exception, asyncio.CancelledError) as e:
            logger.error(f"Scanner failed (attempt {retry_count+1}): {e}")
            logger.info("Attempting restart with backoff...")
            
            # Progressive backoff up to 5 minutes
            backoff = min(2 ** retry_count, max_retry_interval)
            await asyncio.sleep(backoff)
            retry_count += 1
            continue
            
        else:
            # Successful scan - reset retry counter
            retry_count = 0
        finally:
            # Clean up scanner resources
            if scanner:
                await scanner.stop()
                del scanner


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--verbose", "-v", action="store_true", help="Increase the verbosity"
    )
    args = p.parse_args()

    logging.basicConfig(
        stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.WARNING
    )

    logging.debug("Waiting for config...")
    config = json.loads(input())
    logging.debug("Configured: %s", json.dumps(config))
    
    # Get adapter from config with fallback to hci0
    adapter = config.get("adapter", "hci0")
    
    devices: dict[str, ConfiguredDevice] = {}
    for device in config["devices"]:
        devices[device["mac"].lower()] = ConfiguredDevice(
            id=device["id"],
            mac=device["mac"],
            advertisement_key=device["key"],
            secondary_battery=device.get("secondary_battery"),
            link_to_engine=device.get("linkToEngine", False),
            engine_id=device.get("engineId", "mainEngine"),
        )

    logging.info("Starting Victron BLE plugin on adapter %s", adapter)

    # Set all linked engines to 'notRunning' on startup
    for cfg_device in devices.values():
        if cfg_device.link_to_engine:
            delta = {
                "updates": [
                    {
                        "source": {
                            "label": "Victron",
                            "type": "Bluetooth",
                            "src": "system"
                        },
                        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                        "values": [
                            {
                                "path": f"propulsion.{cfg_device.engine_id}.state.value",
                                "value": "stopped",
                                "meta": {"message": "Initial engine state"}
                            }
                        ]
                    }
                ]
            }
            print(json.dumps(delta))
            sys.stdout.flush()

    asyncio.run(monitor(devices, adapter))


if __name__ == "__main__":
    main()
