#     Copyright 2025. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import concurrent
import logging
import logging.config
import logging.handlers
import multiprocessing.managers
import os.path
import subprocess
from copy import deepcopy
from os import execv, listdir, path, pathsep, stat, system
from platform import system as platform_system
from queue import SimpleQueue, Empty
from random import choice
from signal import signal, SIGINT
from string import ascii_lowercase, hexdigits
from sys import argv, executable
from threading import RLock, Thread, main_thread, current_thread, Event
from time import sleep, time, monotonic
from typing import Union, List
from importlib.util import spec_from_file_location, module_from_spec
from simplejson import JSONDecodeError, dumps, load, loads
from yaml import safe_load

from thingsboard_gateway.connectors.connector import Connector
from thingsboard_gateway.gateway.constant_enums import DeviceActions, Status
from thingsboard_gateway.gateway.constants import DEFAULT_CONNECTORS, CONNECTED_DEVICES_FILENAME, CONNECTOR_PARAMETER, \
    PERSISTENT_GRPC_CONNECTORS_KEY_FILENAME, RENAMING_PARAMETER, CONNECTOR_NAME_PARAMETER, DEVICE_TYPE_PARAMETER, \
    CONNECTOR_ID_PARAMETER, ATTRIBUTES_FOR_REQUEST, CONFIG_VERSION_PARAMETER, CONFIG_SECTION_PARAMETER, \
    DEBUG_METADATA_TEMPLATE_SIZE, SEND_TO_STORAGE_TS_PARAMETER, DATA_RETRIEVING_STARTED, ReportStrategy, \
    REPORT_STRATEGY_PARAMETER, DEFAULT_STATISTIC, DEFAULT_DEVICE_FILTER, CUSTOM_RPC_DIR, DISCONNECTED_PARAMETER
from thingsboard_gateway.gateway.device_filter import DeviceFilter
from thingsboard_gateway.gateway.entities.converted_data import ConvertedData
from thingsboard_gateway.gateway.entities.datapoint_key import DatapointKey
from thingsboard_gateway.gateway.entities.report_strategy_config import ReportStrategyConfig
from thingsboard_gateway.gateway.report_strategy.report_strategy_service import ReportStrategyService
from thingsboard_gateway.gateway.shell.proxy import AutoProxy
from thingsboard_gateway.gateway.statistics.decorators import CountMessage, CollectStorageEventsStatistics, \
    CollectAllSentTBBytesStatistics, CollectRPCReplyStatistics
from thingsboard_gateway.gateway.statistics.statistics_service import StatisticsService
from thingsboard_gateway.gateway.tb_client import TBClient
from thingsboard_gateway.storage.file.file_event_storage import FileEventStorage
from thingsboard_gateway.storage.memory.memory_event_storage import MemoryEventStorage
from thingsboard_gateway.storage.sqlite.sqlite_event_storage import SQLiteEventStorage
from thingsboard_gateway.tb_utility.tb_gateway_remote_configurator import RemoteConfigurator
from thingsboard_gateway.tb_utility.tb_handler import TBRemoteLoggerHandler
from thingsboard_gateway.tb_utility.tb_loader import TBModuleLoader
from thingsboard_gateway.tb_utility.tb_logger import TbLogger
from thingsboard_gateway.tb_utility.tb_remote_shell import RemoteShell
from thingsboard_gateway.tb_utility.tb_updater import TBUpdater
from thingsboard_gateway.tb_utility.tb_utility import TBUtility

GRPC_LOADED = False
try:
    from thingsboard_gateway.gateway.grpc_service.grpc_connector import GrpcConnector
    from thingsboard_gateway.gateway.grpc_service.tb_grpc_manager import TBGRPCServerManager

    GRPC_LOADED = True
except ImportError:

    class GrpcConnector:
        pass


    class TBGRPCServerManager:
        pass

logging.setLoggerClass(TbLogger)
log: TbLogger = None  # type: ignore


def load_file(path_to_file):
    with open(path_to_file, 'r') as target_file:
        content = load(target_file)
    return content


class GatewayManager(multiprocessing.managers.BaseManager):
    def __init__(self, address=None, authkey=b''):
        super().__init__(address=address, authkey=authkey)
        self.gateway = None

    def has_gateway(self):
        return self.gateway is not None

    def add_gateway(self, gateway):
        self.gateway = gateway

    def shutdown(self) -> None:
        super().__exit__(None, None, None)


class TBGatewayService:
    DEFAULT_TIMEOUT = 5

    EXPOSED_GETTERS = [
        'ping',
        'get_status',
        'get_storage_name',
        'get_storage_events_count',
        'get_available_connectors',
        'get_connector_status',
        'get_connector_config'
    ]

    def __init__(self, config_file=None):
        logging.setLoggerClass(TbLogger)
        self.__init_variables()
        if current_thread() is main_thread():
            signal(SIGINT, lambda _, __: self.__stop_gateway())

        self.__lock = RLock()
        self.__process_async_actions_thread = Thread(target=self.__process_async_device_actions,
                                                     name="Async device actions processing thread", daemon=True)

        self._config_dir = path.dirname(path.abspath(config_file)) + path.sep
        if config_file is None:
            config_file = (path.dirname(path.dirname(path.abspath(__file__))) +
                           '/config/tb_gateway.json'.replace('/', path.sep))

        logging_error = None
        try:
            with open(self._config_dir + 'logs.json', 'r') as file:
                log_config = load(file)

            TbLogger.check_and_update_file_handlers_class_name(log_config)
            logging.config.dictConfig(log_config)
        except Exception as e:
            logging_error = e

        global log
        log = logging.getLogger('service')
        if logging_error is not None:
            log.setLevel('INFO')

        # load general configuration YAML/JSON
        self.__config = self.__load_general_config(config_file)

        # change main config if Gateway running with env variables
        self.__config = TBUtility.update_main_config_with_env_variables(self.__config)

        log.info("Gateway starting...")
        storage_log = logging.getLogger('storage')
        self._event_storage = self._event_storage_types[self.__config["storage"]["type"]](self.__config["storage"],
                                                                                          storage_log,
                                                                                          self.stop_event)
        if self.__config['thingsboard'].get('reportStrategy', {}).get('type') != "DISABLED":
            self._report_strategy_service = ReportStrategyService(self.__config['thingsboard'],
                                                                  self,
                                                                  self.__converted_data_queue,
                                                                  log)
        else:
            self._report_strategy_service = None
        self.__updater = TBUpdater()
        self.version = self.__updater.get_version()
        log.info("ThingsBoard IoT gateway version: %s", self.version["current_version"])
        self.name = ''.join(choice(ascii_lowercase) for _ in range(64))

        self.__latency_debug_mode = self.__config['thingsboard'].get('latencyDebugMode', False)

        self.__sync_devices_shared_attributes_on_connect = self.__config['thingsboard'].get('syncDevicesSharedAttributesOnConnect', True)

        self.__connectors_not_found = False
        self._load_connectors()
        self.__connectors_init_start_success = True

        self.__load_persistent_devices()
        try:
            self.__connect_with_connectors()
        except Exception as e:
            log.info("Initial connection was not success, waiting for remote configuration")
            log.debug("Initial connection failed with error: %s", e)
            self.__connectors_init_start_success = False

        connection_logger = logging.getLogger('tb_connection')
        self.tb_client = TBClient(self.__config["thingsboard"], self._config_dir, connection_logger)
        self.tb_client.register_service_subscription_callback(self.subscribe_to_required_topics)
        self.tb_client.connect()
        if self.stopped:
            return
        if logging_error is not None:
            self.send_telemetry({"ts": time() * 1000, "values": {
                "LOGS": "Logging loading exception, logs.json is wrong: %s" % (str(logging_error),)}})
            TBRemoteLoggerHandler.set_default_handler()
        if not hasattr(self, "remote_handler"):
            self.remote_handler = TBRemoteLoggerHandler(self)

        self.__debug_log_enabled = log.isEnabledFor(10)
        self.update_loggers()
        self.__save_converted_data_thread = Thread(name="Storage fill thread", daemon=True,
                                                   target=self.__send_to_storage)
        self.__save_converted_data_thread.start()

        self.init_remote_shell(self.__config["thingsboard"].get("remoteShell"))
        self.__rpc_processing_thread = Thread(target=self.__send_rpc_reply_processing, daemon=True,
                                              name="RPC processing thread")
        self.__rpc_processing_thread.start()
        self.__rpc_to_devices_processing_thread = Thread(target=self.__rpc_to_devices_processing, daemon=True,
                                                         name="RPC to devices processing thread")
        self.__rpc_to_devices_processing_thread.start()

        self.__process_sync_device_shared_attrs_thread = Thread(target=self.__sync_device_shared_attrs_loop, daemon=True,
                                                                name="Sync device shared attributes thread")
        self.__process_sync_device_shared_attrs_thread.start()

        self.init_grpc_service(self.__config.get('grpc'))

        self.__devices_idle_checker = self.__config['thingsboard'].get('checkingDeviceActivity', {})
        self.__check_devices_idle = self.__devices_idle_checker.get('checkDeviceInactivity', False)
        if self.__check_devices_idle:
            thread = Thread(name='Checking devices idle time', target=self.__check_devices_idle_time, daemon=True)
            thread.start()
            log.info('Start checking devices idle time')

        self.init_statistics_service(self.__config['thingsboard'].get('statistics', DEFAULT_STATISTIC))

        self.__min_pack_send_delay_ms = self.__config['thingsboard'].get('minPackSendDelayMS', 50)
        self.__min_pack_send_delay_ms = self.__min_pack_send_delay_ms / 1000.0
        self.__min_pack_size_to_send = self.__config['thingsboard'].get('minPackSizeToSend', 500)
        self.__max_payload_size_in_bytes = self.__config["thingsboard"].get("maxPayloadSizeBytes", 8196)

        self._send_thread = Thread(target=self.__read_data_from_storage, daemon=True,
                                   name="Send data to Thingsboard Thread")
        self._send_thread.start()

        self.init_device_filtering(self.__config['thingsboard'].get('deviceFiltering', DEFAULT_DEVICE_FILTER))

        log.info("Gateway core started.")

        self._watchers_thread = Thread(target=self._watchers, name='Watchers', daemon=True)
        self._watchers_thread.start()

        self.__init_remote_configuration()

        if self.__connectors_not_found:
            self.connectors_configs = {}
            self.load_connectors()

        try:
            if not self.__connectors_init_start_success:
                self.connect_with_connectors()
        except Exception as e:
            log.info("Error while connecting to connectors, please update configuration: %s", e)

        log.info("Gateway connectors initialized.")

        self.__load_persistent_devices()

        log.info("Persistent devices loaded.")

        if self.__config['thingsboard'].get('managerEnabled', False):
            manager_address = '/tmp/gateway'
            if path.exists('/tmp/gateway'):
                try:
                    # deleting old manager if it was closed incorrectly
                    system('rm -rf /tmp/gateway')
                except OSError as e:
                    log.error("Failed to remove old manager: %s", exc_info=e)
                manager_address = '/tmp/gateway'
            if platform_system() == 'Windows':
                manager_address = ('127.0.0.1', 9999)
            self.manager = GatewayManager(address=manager_address, authkey=b'gateway')

            if current_thread() is main_thread():
                GatewayManager.register('get_gateway',
                                        self.get_gateway,
                                        proxytype=AutoProxy,
                                        exposed=self.EXPOSED_GETTERS,
                                        create_method=False)
                self.server = self.manager.get_server()
                self.server.serve_forever()

        log.info("Gateway started.")

        while not self.stopped:
            try:
                self.stop_event.wait(1)
            except KeyboardInterrupt:
                self.__stop_gateway()
                break

    def __init_variables(self):
        self.stopped = False
        self.stop_event = Event()
        self.__device_filter_config = None
        self.__device_filter = None
        self.__grpc_manager = None
        self.__remote_shell = None
        self.__statistics = None
        self.__statistics_service = None
        self.__grpc_config = None
        self.__grpc_connectors = None
        self.__grpc_manager = None
        self.__remote_configurator = None
        self.tb_client = None
        self.__requested_config_after_connect = False
        self.__rpc_reply_sent = False
        self.__subscribed_to_rpc_topics = False
        self.__rpc_remote_shell_command_in_progress = None
        self.connectors_configs = {}
        self.__scheduled_rpc_calls = []
        self.__rpc_requests_in_progress = {}
        self.available_connectors_by_name: dict[str, Connector] = {}
        self.available_connectors_by_id: dict[str, Connector] = {}
        self.__devices_shared_attributes = {}
        self.__connector_incoming_messages = {}
        self.__connected_devices = {}
        self.__renamed_devices = {}
        self.__saved_devices = {}
        self.__added_devices = {}
        self.__disconnected_devices = {}
        self.__events = []
        self.__grpc_connectors = {}
        self._default_connectors = DEFAULT_CONNECTORS

        self._published_events = SimpleQueue()
        self.__rpc_processing_queue = SimpleQueue()
        self.__rpc_to_devices_queue = SimpleQueue()
        self.__async_device_actions_queue = SimpleQueue()
        self.__rpc_register_queue = SimpleQueue()
        self.__converted_data_queue = SimpleQueue()
        self.__sync_device_shared_attrs_queue = SimpleQueue()

        self.__messages_confirmation_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4) # noqa

        self.__updates_check_period_ms = 300000
        self.__updates_check_time = 0

        self._implemented_connectors = {}
        self._event_storage_types = {
            "memory": MemoryEventStorage,
            "file": FileEventStorage,
            "sqlite": SQLiteEventStorage,
        }
        self.__gateway_rpc_methods = {
            "ping": self.__rpc_ping,
            "stats": self.__form_statistics,
            "devices": self.__rpc_devices,
            "update": self.__rpc_update,
            "version": self.__rpc_version,
            "device_renamed": self.__process_renamed_gateway_devices,
            "device_deleted": self.__process_deleted_gateway_devices,
        }
        self.load_custom_rpc_methods(CUSTOM_RPC_DIR)
        self.__rpc_scheduled_methods_functions = {
            "restart": {"function": execv, "arguments": (executable, [executable.split(pathsep)[-1]] + argv)},
            "reboot": {"function": subprocess.call, "arguments": (["shutdown", "-r", "-t", "0"],)},
        }
        self.async_device_actions = {
            DeviceActions.CONNECT: self.add_device,
            DeviceActions.DISCONNECT: self.del_device
        }

    @staticmethod
    def __load_general_config(config_file):
        file_extension = config_file.split('.')[-1]
        if file_extension == 'json' and os.path.exists(config_file):
            with open(config_file) as general_config:
                try:
                    return load(general_config)
                except Exception as e:
                    log.error('Failed to load configuration file:\n %s', exc_info=e)
        else:
            log.warning('YAML configuration is deprecated. '
                        'Please, use JSON configuration instead.')
            log.warning(
                'See default configuration on '
                'https://thingsboard.io/docs/iot-gateway/configuration/')

            config = {}
            try:
                filename = ''.join(config_file.split('.')[:-1])
                with open(filename + '.yaml') as general_config:
                    config = safe_load(general_config)

                with open(filename + '.json', 'w') as file:
                    file.writelines(dumps(config, indent='  '))
            except Exception as e:
                log.error('Failed to load configuration file:\n %s', exc_info=e)

            return config

    def init_grpc_service(self, config):
        self.__grpc_config = config # noqa
        if GRPC_LOADED and self.__grpc_config is not None and self.__grpc_config.get("enabled"):
            self.__process_async_actions_thread.start()
            self.__grpc_manager = TBGRPCServerManager(self, self.__grpc_config) # noqa
            self.__grpc_manager.set_gateway_read_callbacks(self.__register_connector, self.__unregister_connector)

    def init_statistics_service(self, config):
        self.__statistics = config # noqa
        if isinstance(self.__statistics_service, StatisticsService):
            self.__statistics_service.stop()
        if self.__statistics.get('enable', False) or self.__statistics.get('enableCustom', False):
            if self.__statistics.get('enable', False):
                StatisticsService.enable_statistics()
                log.debug('General statistics enabled')
            else:
                StatisticsService.disable_statistics()
                log.debug('General statistics disabled')
            if self.__statistics.get('enableCustom', True):
                StatisticsService.enable_custom_statistics()
                log.debug('Custom statistics enabled')
            else:
                StatisticsService.disable_custom_statistics()
                log.debug('Custom statistics disabled')
            self.__statistics_service = StatisticsService(self.__statistics, self, log,
                                                          config_path=self._config_dir + self.__statistics[
                                                              'configuration'] if self.__statistics.get(
                                                              'configuration') else None)
            log.debug('Statistics service initialized')
        else:
            self.__statistics_service = None # noqa
            StatisticsService.disable_statistics()
            StatisticsService.disable_custom_statistics()
            log.debug('Statistics service disabled')

    def init_device_filtering(self, config):
        self.__device_filter_config = config  # noqa
        self.__device_filter = None
        if self.__device_filter_config['enable'] and self.__device_filter_config.get('filterFile'):
            self.__device_filter = DeviceFilter(config_path=self._config_dir + self.__device_filter_config['filterFile']) # noqa

    def init_remote_shell(self, enable):
        self.__remote_shell = None
        if enable:
            log.warning("Remote shell is enabled. Please be carefully with this feature.")
            self.__remote_shell = RemoteShell(platform=self.__updater.get_platform(),
                                              release=self.__updater.get_release(),
                                              logger=log) # noqa

    @property
    def event_storage_types(self):
        return self._event_storage_types

    @property
    def config(self):
        return self.__config

    @config.setter
    def config(self, config):
        self.__config.update(config)

    @property
    def connected_devices(self):
        return len(self.__connected_devices.keys())

    @property
    def active_connectors(self):
        return len(self.available_connectors_by_id.keys())

    @property
    def inactive_connectors(self):
        return len(self.connectors_configs.keys()) - len(self.available_connectors_by_id.keys())

    @property
    def total_connectors(self):
        return len(self.connectors_configs.keys())

    def get_gateway(self):
        if self.manager.has_gateway():
            return self.manager.gateway
        else:
            self.manager.add_gateway(self)
            self.manager.register('gateway', lambda: self, proxytype=AutoProxy)

    def _watchers(self):
        global log
        try:
            connectors_configuration_check_time = 0
            logs_sending_check_time = 0
            update_logger_time = 0

            while not self.stopped:
                try:
                    cur_time = time() * 1000

                    if not self.tb_client.is_connected() and self.__subscribed_to_rpc_topics:
                        self.__subscribed_to_rpc_topics = False
                        self.__devices_shared_attributes = {}

                    if (not self.tb_client.is_connected()
                            and self.__remote_configurator is not None
                            and self.__requested_config_after_connect):
                        self.__requested_config_after_connect = False

                    if (self.tb_client.is_connected()
                            and not self.tb_client.is_stopped()
                            and not self.__subscribed_to_rpc_topics):
                        for device in list(self.__saved_devices.keys()):
                            self.add_device(device,
                                            {CONNECTOR_PARAMETER: self.__saved_devices[device][CONNECTOR_PARAMETER]},
                                            device_type=self.__saved_devices[device][DEVICE_TYPE_PARAMETER])
                        self.subscribe_to_required_topics()

                    if self.__scheduled_rpc_calls:
                        for rpc_call_index in range(len(self.__scheduled_rpc_calls)):
                            rpc_call = self.__scheduled_rpc_calls[rpc_call_index]
                            if cur_time > rpc_call[0]:
                                rpc_call = self.__scheduled_rpc_calls.pop(rpc_call_index)
                                result = None
                                try:
                                    result = rpc_call[1]["function"](*rpc_call[1]["arguments"])
                                except Exception as e:
                                    log.error("Error while executing scheduled RPC call: %s", exc_info=e)

                                if result == 256:
                                    log.warning("Error on RPC command: 256. Permission denied.")

                    if ((self.__rpc_requests_in_progress or not self.__rpc_register_queue.empty())
                            and self.tb_client.is_connected()):
                        try:
                            new_rpc_request_in_progress = {}
                            if self.__rpc_requests_in_progress:
                                for rpc_in_progress, data in self.__rpc_requests_in_progress.items():
                                    if cur_time >= data[1]:
                                        data[2](rpc_in_progress)
                                        self.cancel_rpc_request(rpc_in_progress)
                                        self.__rpc_requests_in_progress[rpc_in_progress] = "del"
                                new_rpc_request_in_progress = {key: value for key, value in
                                                               self.__rpc_requests_in_progress.items() if value != 'del'
                                                               }
                            if not self.__rpc_register_queue.empty():
                                new_rpc_request_in_progress = self.__rpc_requests_in_progress
                                rpc_request_from_queue = self.__rpc_register_queue.get(False)
                                topic = rpc_request_from_queue["topic"]
                                data = rpc_request_from_queue["data"]
                                new_rpc_request_in_progress[topic] = data
                            self.__rpc_requests_in_progress = new_rpc_request_in_progress
                        except Exception as e:
                            log.error("Error while processing RPC requests: %s", exc_info=e)
                            self.stop_event.wait(1)
                    else:
                        try:
                            self.stop_event.wait(.02)
                        except Exception as e:
                            log.error("Error in main loop: %s", exc_info=e)
                            break

                    if (not self.__requested_config_after_connect and self.tb_client.is_connected()
                            and self.tb_client.is_subscribed_to_service_attributes()):
                        self.__requested_config_after_connect = True
                        self._check_shared_attributes()

                    if (cur_time - connectors_configuration_check_time > self.__config["thingsboard"].get("checkConnectorsConfigurationInSeconds", 60) * 1000 # noqa
                            and not (self.__remote_configurator is not None and self.__remote_configurator.in_process)):
                        self.check_connector_configuration_updates()
                        connectors_configuration_check_time = time() * 1000

                    if cur_time - self.__updates_check_time >= self.__updates_check_period_ms:
                        self.__updates_check_time = time() * 1000
                        self.version = self.__updater.get_version()

                    if cur_time - logs_sending_check_time >= 1000:
                        logs_sending_check_time = time() * 1000
                        TbLogger.send_errors_if_needed(self)

                    if cur_time - update_logger_time > 60000:
                        update_logger_time = cur_time
                        log = logging.getLogger('service')
                        self.__debug_log_enabled = log.isEnabledFor(10)

                    self.stop_event.wait(.1)
                except Exception as e:
                    log.error("Error in main loop: %s", exc_info=e)
                    self.stop_event.wait(1)
        except Exception as e:
            log.error("Error in main loop: %s", exc_info=e)
            self.__stop_gateway()
            self.__close_connectors()
            log.info("The gateway has been stopped.")
            self.tb_client.stop()

    def __close_connectors(self):
        for current_connector in self.available_connectors_by_id:
            try:
                close_start = monotonic()
                while not self.available_connectors_by_id[current_connector].is_stopped():
                    self.available_connectors_by_id[current_connector].close()
                    if self.tb_client.is_connected():
                        for device in self.get_connector_devices(self.available_connectors_by_id[current_connector]):
                            self.del_device(device, False)
                    if monotonic() - close_start > 5:
                        log.error("Connector %s close timeout", current_connector)
                        break
                log.debug("Connector %s closed connection.", current_connector)
            except Exception as e:
                log.error("Error while closing connector %s", current_connector, exc_info=e)

    def __stop_gateway(self):
        self.stopped = True
        self.stop_event.set()
        if hasattr(self, "_TBGatewayService__updater") and self.__updater is not None:
            self.__updater.stop()
        log.info("Stopping...")

        if hasattr(self, "_TBGatewayService__statistics_service") and self.__statistics_service is not None:
            self.__statistics_service.stop()

        if hasattr(self, "_TBGatewayService__grpc_manager") and self.__grpc_manager is not None:
            self.__grpc_manager.stop()
        if os.path.exists("/tmp/gateway"):
            os.remove("/tmp/gateway")
        self.__close_connectors()
        if hasattr(self, "_event_storage") and self._event_storage is not None:
            self._event_storage.stop()
        log.info("The gateway has been stopped.")
        if hasattr(self, 'remote_handler'):
            self.remote_handler.deactivate()
        if hasattr(self, "_TBGatewayService__messages_confirmation_executor") is not None:
            self.__messages_confirmation_executor.shutdown(wait=True, cancel_futures=True)
        if hasattr(self, "tb_client") and self.tb_client is not None:
            self.tb_client.disconnect()
            self.tb_client.stop()
        if hasattr(self, "manager") and self.manager is not None:
            self.manager.shutdown()
        for logger in logging.Logger.manager.loggerDict:
            if isinstance(logger, TbLogger):
                logger.stop()

    def __init_remote_configuration(self, force=False):
        remote_configuration_enabled = self.__config["thingsboard"].get("remoteConfiguration")
        if not remote_configuration_enabled and force:
            log.info("Remote configuration is enabled forcibly!")
        if (remote_configuration_enabled or force) and self.__remote_configurator is None:
            try:
                self.__remote_configurator = RemoteConfigurator(self, self.__config)

                while (not self.tb_client.is_connected() and not self.tb_client.client.get_subscriptions_in_progress()
                       and not self.stopped):
                    self.stop_event.wait(1)

                self._check_shared_attributes(shared_keys=[])
            except Exception as e:
                log.error("Failed to initialize remote configuration: %s", e)
        if self.__remote_configurator is not None:
            self.__remote_configurator.send_current_configuration()

    @CountMessage('msgsReceivedFromPlatform')
    def _attributes_parse(self, content, *args):
        try:
            log.trace("Received data: %s, %s", content, args)
            if content is not None:
                shared_attributes = content.get("shared", {})
                client_attributes = content.get("client", {})
                if shared_attributes or client_attributes:
                    self.__process_attributes_response(shared_attributes, client_attributes)
                else:
                    self.__process_attribute_update(content)

                if shared_attributes:
                    log.trace("Shared attributes received (%s).",
                              ", ".join([attr for attr in shared_attributes.keys()]))
                if client_attributes:
                    log.trace("Client attributes received (%s).",
                              ", ".join([attr for attr in client_attributes.keys()]))
        except Exception as e:
            log.error("Failed to process attributes: %s", e)

    def __process_attribute_update(self, content):
        self.__process_remote_logging_update(content.get("RemoteLoggingLevel"))
        self.__process_remote_configuration(content)
        self.__process_remote_converter_configuration_update(content)

    def __process_attributes_response(self, shared_attributes, client_attributes):
        if shared_attributes:
            log.trace("Shared attributes received: %s", shared_attributes)
        if client_attributes:
            log.trace("Client attributes received: %s", client_attributes)
        self.__process_remote_logging_update(shared_attributes.get('RemoteLoggingLevel'))
        self.__process_remote_configuration(shared_attributes)

    def __process_remote_logging_update(self, remote_logging_level):
        if remote_logging_level is not None:
            remote_logging_level_id = TBRemoteLoggerHandler.get_logger_level_id(remote_logging_level.upper())
            self.remote_handler.activate_remote_logging_for_level(remote_logging_level_id)
            log.info('Remote logging level set to: %s ', remote_logging_level)

    def __process_remote_converter_configuration_update(self, content: dict):
        try:
            key = list(content.keys())[0]
            connector_name, converter_name = key.split(':')
            log.info('Got remote converter configuration update')
            if not self.available_connectors_by_name.get(connector_name):
                raise ValueError
        except (ValueError, AttributeError, IndexError) as e:
            log.trace('Failed to process remote converter update: %s', e)

    def update_connector_config_file(self, connector_name, config):
        for connector in self.__config['connectors']:
            if connector['name'] == connector_name:
                self.__save_connector_config_file(connector['configuration'], config)

                log.info('Updated %s configuration file', connector_name)
                return

    def __save_connector_config_file(self, connector_filename, config):
        with open(self._config_dir + connector_filename, 'w', encoding='UTF-8') as file:
            file.writelines(dumps(config, indent=4))

    def __process_deleted_gateway_devices(self, deleted_device_name: str):
        log.info("Received deleted gateway device notification: %s", deleted_device_name)
        if deleted_device_name in list(self.__renamed_devices.values()):
            first_device_name = TBUtility.get_dict_key_by_value(self.__renamed_devices, deleted_device_name)
            del self.__renamed_devices[first_device_name]
            deleted_device_name = first_device_name
            log.debug("Current renamed_devices dict: %s", self.__renamed_devices)
        if deleted_device_name in self.__connected_devices:
            del self.__connected_devices[deleted_device_name]
            log.debug("Device %s - was removed from __connected_devices", deleted_device_name)
        if deleted_device_name in self.__saved_devices:
            del self.__saved_devices[deleted_device_name]
            log.debug("Device %s - was removed from __saved_devices", deleted_device_name)
        if deleted_device_name in self.__added_devices:
            del self.__added_devices[deleted_device_name]
            log.debug("Device %s - was removed from __added_devices", deleted_device_name)
        if hasattr(self, "__duplicate_detector"):
            self.__duplicate_detector.delete_device(deleted_device_name)
        self.__disconnected_devices.pop(deleted_device_name, None)
        self.__save_persistent_devices()
        self.__load_persistent_devices()
        return {'success': True}

    def __process_renamed_gateway_devices(self, renamed_device: dict):
        if self.__config.get('handleDeviceRenaming', True):
            log.info("Received renamed gateway device notification: %s", renamed_device)
            old_device_name, new_device_name = list(renamed_device.items())[0]
            if old_device_name in list(self.__renamed_devices.values()):
                device_name_key = TBUtility.get_dict_key_by_value(self.__renamed_devices, old_device_name)
                if device_name_key == new_device_name:
                    self.__renamed_devices.pop(device_name_key, None)
                    device_name_key = None
            else:
                device_name_key = old_device_name

            if device_name_key is not None and device_name_key != new_device_name:
                self.__renamed_devices[device_name_key] = new_device_name

            self.__save_persistent_devices()
            self.__load_persistent_devices()
            log.debug("Current renamed_devices dict: %s", self.__renamed_devices)
        else:
            log.debug("Received renamed device notification %r, but device renaming handle is disabled",
                      renamed_device)
        return {'success': True}

    @staticmethod
    def __process_remote_configuration(new_configuration):
        if new_configuration is not None:
            try:
                RemoteConfigurator.RECEIVED_UPDATE_QUEUE.put(new_configuration)
            except Exception as e:
                log.error("Failed to process remote configuration: %s", e)

    def get_config_path(self):
        return self._config_dir

    def subscribe_to_required_topics(self):
        if not self.__subscribed_to_rpc_topics and self.tb_client.is_connected():
            self.tb_client.client.clean_device_sub_dict()
            self.tb_client.client.gw_set_server_side_rpc_request_handler(self._rpc_request_handler)
            self.tb_client.client.set_server_side_rpc_request_handler(self._rpc_request_handler)
            self.tb_client.client.subscribe_to_all_attributes(self._attribute_update_callback)
            self.tb_client.client.gw_subscribe_to_all_attributes(self._attribute_update_callback)
            self.__subscribed_to_rpc_topics = True # noqa

    def request_device_attributes(self, device_name, shared_keys, client_keys, callback):
        if client_keys is not None:
            self.tb_client.client.gw_request_client_attributes(device_name, client_keys, callback)
        if shared_keys is not None:
            # TODO: Add caching for shared attributes to __devices_shared_attributes
            # TODO: Refactor connectors to use this method to request attributes
            self.tb_client.client.gw_request_shared_attributes(device_name, shared_keys, callback)

    def _check_shared_attributes(self, shared_keys=None, client_keys=None):
        if shared_keys is None:
            shared_keys = ATTRIBUTES_FOR_REQUEST
        self.tb_client.client.request_attributes(callback=self._attributes_parse,
                                                 shared_keys=shared_keys,
                                                 client_keys=client_keys)

    def __register_connector(self, session_id, connector_key):
        if (self.__grpc_connectors.get(connector_key) is not None
                and self.__grpc_connectors[connector_key]['id'] not in self.available_connectors_by_id):
            target_connector = self.__grpc_connectors.get(connector_key)
            connector = GrpcConnector(self, target_connector['config'], self.__grpc_manager, session_id)
            connector.setName(target_connector['name'])
            self.available_connectors_by_name[connector.get_name()] = connector
            self.available_connectors_by_id[connector.get_id()] = connector
            self.__grpc_manager.registration_finished(Status.SUCCESS, session_id, target_connector)
            log.info("[%r][%r] GRPC connector with key %s registered with name %s", session_id,
                     connector.get_id(), connector_key, connector.get_name())
        elif self.__grpc_connectors.get(connector_key) is not None:
            self.__grpc_manager.registration_finished(Status.FAILURE, session_id, None)
            log.error("[%r] GRPC connector with key: %s - already registered!", session_id, connector_key)
        else:
            self.__grpc_manager.registration_finished(Status.NOT_FOUND, session_id, None)
            log.error("[%r] GRPC configuration for connector with key: %s - not found", session_id,
                      connector_key)

    def __unregister_connector(self, session_id, connector_key):
        if (self.__grpc_connectors.get(connector_key) is not None
                and self.__grpc_connectors[connector_key]['id'] in self.available_connectors_by_id):
            connector_id = self.__grpc_connectors[connector_key]['id']
            target_connector = self.available_connectors_by_id.pop(connector_id)
            self.__grpc_manager.unregister(Status.SUCCESS, session_id, target_connector)
            log.info("[%r] GRPC connector with key %s and name %s - unregistered", session_id, connector_key,
                     target_connector.get_name())
        elif self.__grpc_connectors.get(connector_key) is not None:
            self.__grpc_manager.unregister(Status.NOT_FOUND, session_id, None)
            log.error("[%r] GRPC connector with key: %s - is not registered!", session_id, connector_key)
        else:
            self.__grpc_manager.unregister(Status.FAILURE, session_id, None)
            log.error("[%r] GRPC configuration for connector with key: %s - not found and not registered",
                      session_id, connector_key)

    @staticmethod
    def _generate_persistent_key(connector, connectors_persistent_keys):
        if connectors_persistent_keys and connectors_persistent_keys.get(connector['name']) is not None:
            connector_persistent_key = connectors_persistent_keys[connector['name']]
        else:
            connector_persistent_key = "".join(choice(hexdigits) for _ in range(10))
            connectors_persistent_keys[connector['name']] = connector_persistent_key

        return connector_persistent_key

    def load_connectors(self, config=None):
        self._load_connectors(config=config)

    def _load_connectors(self, config=None):
        global log
        self.connectors_configs = {}
        connectors_persistent_keys = self.__load_persistent_connector_keys()

        if config:
            connectors_configuration_in_main_config = config.get('connectors')
        else:
            connectors_configuration_in_main_config = self.__config.get('connectors')

        if connectors_configuration_in_main_config:
            for connector_config_from_main in connectors_configuration_in_main_config:
                try:
                    connector_persistent_key = None
                    connector_type = connector_config_from_main["type"].lower() \
                        if connector_config_from_main.get("type") is not None else None

                    if connector_type is None:
                        log.error("Connector type is not defined!")
                        continue
                    if connector_type == "grpc" and self.__grpc_manager is None:
                        log.error("Cannot load connector with name: %s and type grpc. GRPC server is disabled!",
                                  connector_config_from_main['name'])
                        continue

                    if connector_type != "grpc":
                        connector_class = None
                        if connector_config_from_main.get('useGRPCForce', False):
                            module_name = f'Grpc{self._default_connectors.get(connector_type, connector_config_from_main.get("class"))}' # noqa
                            connector_class = TBModuleLoader.import_module(connector_type, module_name)

                        if self.__grpc_manager and self.__grpc_manager.is_alive() and connector_class:
                            connector_persistent_key = self._generate_persistent_key(connector_config_from_main,
                                                                                     connectors_persistent_keys)
                        else:
                            connector_class = TBModuleLoader.import_module(connector_type,
                                                                           self._default_connectors.get(
                                                                               connector_type,
                                                                               connector_config_from_main.get('class')))

                        if connector_class is not None and isinstance(connector_class, list):
                            log.warning("Connector implementation not found for %s",
                                        connector_config_from_main['name'])
                            for error in connector_class:
                                log.error("The following error occurred during importing connector class: %s",
                                          error, exc_info=error)
                            continue
                        elif connector_class is None:
                            log.error("Connector implementation not found for %s",
                                      connector_config_from_main['name'])
                            continue
                        else:
                            self._implemented_connectors[connector_type] = connector_class
                    elif connector_type == "grpc":
                        if connector_config_from_main.get('key') == "auto":
                            self._generate_persistent_key(connector_config_from_main, connectors_persistent_keys)
                        else:
                            connector_persistent_key = connector_config_from_main['key']
                        log.info("Connector key for GRPC connector with name [%s] is: [%s]",
                                 connector_config_from_main['name'],
                                 connector_persistent_key)
                    connector_config_file_path = self._config_dir + connector_config_from_main['configuration']

                    if not path.exists(connector_config_file_path):
                        log.error("Configuration file for connector with name: %s not found!",
                                  connector_config_from_main['name'])
                        continue
                    with open(connector_config_file_path, 'r', encoding="UTF-8") as conf_file:
                        connector_conf_file_data = conf_file.read()

                    connector_conf_from_file = connector_conf_file_data
                    try:
                        connector_conf_from_file = loads(connector_conf_file_data)
                    except JSONDecodeError as e:
                        log.debug(e)
                        log.warning("Cannot parse connector configuration as a JSON, it will be passed as a string.")

                    connector_id = TBUtility.get_or_create_connector_id(connector_conf_from_file)

                    if isinstance(connector_conf_from_file, dict):
                        if connector_conf_from_file.get('id') is None:
                            connector_conf_from_file['id'] = connector_id
                            with open(connector_config_file_path, 'w', encoding="UTF-8") as conf_file:
                                conf_file.write(dumps(connector_conf_from_file, indent=2))
                    elif isinstance(connector_conf_from_file, str) and not connector_conf_from_file:
                        raise ValueError("Connector configuration is empty!")
                    elif isinstance(connector_conf_from_file, str):
                        start_find = connector_conf_from_file.find("{id_var_start}")
                        end_find = connector_conf_from_file.find("{id_var_end}")
                        if not (start_find > -1 and end_find > -1):
                            connector_conf_from_file = ("{id_var_start}" + str(connector_id) + "{id_var_end}" +
                                                        connector_conf_from_file)

                    if not self.connectors_configs.get(connector_type):
                        self.connectors_configs[connector_type] = []
                    if connector_type != 'grpc' and isinstance(connector_conf_from_file, dict):
                        connector_conf_from_file["name"] = connector_config_from_main['name']
                    if connector_type != 'grpc':
                        connector_configuration = {
                            connector_config_from_main['configuration']: connector_conf_from_file}
                    else:
                        connector_configuration = connector_conf_from_file
                    connector_config_version = connector_configuration.get(CONFIG_VERSION_PARAMETER) if isinstance(
                        connector_configuration, dict) else None
                    connector_configuration_local = {"name": connector_config_from_main['name'],
                                                     "id": connector_id,
                                                     "config": connector_configuration,
                                                     CONFIG_VERSION_PARAMETER: connector_config_version,
                                                     "config_updated": stat(connector_config_file_path),
                                                     "config_file_path": connector_config_file_path,
                                                     "grpc_key": connector_persistent_key}
                    if isinstance(connector_conf_from_file, dict) and connector_conf_from_file.get(REPORT_STRATEGY_PARAMETER) is not None: # noqa
                        connector_configuration_local[REPORT_STRATEGY_PARAMETER] = connector_conf_from_file[REPORT_STRATEGY_PARAMETER] # noqa
                    self.connectors_configs[connector_type].append(connector_configuration_local)
                except Exception as e:
                    log.error("Error on loading connector: %r", e)
                    log.debug("Error on loading connector:", exc_info=e)
            if connectors_persistent_keys:
                self.__save_persistent_keys(connectors_persistent_keys)
        else:
            log.info("Connectors - not found, waiting for remote configuration.")
            if self.tb_client is not None and self.tb_client.is_connected():
                self.__init_remote_configuration(force=True)
            else:
                self.__connectors_not_found = True

    def connect_with_connectors(self):
        self.__connect_with_connectors()

    def __update_connector_devices(self, connector):
        for device_name in set(self.__connected_devices.keys()):
            device = self.__connected_devices[device_name]
            if (device.get('connector') and
                    (device['connector'].name == connector.name or device['connector'].get_id() == connector.get_id())):
                self.update_device(device_name, 'connector', connector)

    def clean_shared_attributes_cache_for_connector_devices(self, connector):
        connector_devices = self.get_connector_devices(connector)
        for device_name in connector_devices:
            self.__devices_shared_attributes.pop(device_name, None)

    def get_connector_devices(self, connector):
        return [device_name for device_name, device in self.__connected_devices.items() if device.get('connector') and device['connector'].get_id() == connector.get_id()]  # noqa

    def __cleanup_connectors(self):
        self.available_connectors_by_id = {connector_id: connector for (connector_id, connector) in
                                           self.available_connectors_by_id.items() if not connector.is_stopped()}

    def __connect_with_connectors(self):
        global log
        for connector_type in self.connectors_configs:
            connector_type = connector_type.lower()
            for connector_config in self.connectors_configs[connector_type]:
                if self._implemented_connectors.get(connector_type) is not None:

                    if connector_type == 'grpc' or 'Grpc' in self._implemented_connectors[connector_type].__name__:
                        self.__init_and_start_grpc_connector(connector_type, connector_config)
                        return

                    for config_file_name in connector_config[CONFIG_SECTION_PARAMETER]:
                        connector = None
                        connector_name = None
                        connector_id = None
                        connector_configuration = connector_config[CONFIG_SECTION_PARAMETER].get(config_file_name)
                        try:
                            connector_name = connector_config["name"]
                            connector_id = connector_config["id"]
                            connector = self.__init_and_start_regular_connector(connector_id,
                                                                                connector_type,
                                                                                connector_name,
                                                                                connector_configuration)
                        except Exception as e:
                            log.error("[%r] Error on loading connector %r: %s", connector_id, connector_name, e)
                            if isinstance(log, TbLogger):
                                log.error("Error on loading connector %r: %s", connector_name, e, attr_name=connector_name)
                            else:
                                log.error("Error on loading connector %r: %s", connector_name, e)
                                log.debug("Error on loading connector %r", connector_name, exc_info=e)
                            if connector is not None and not connector.is_stopped():
                                connector.close()
                                if self.tb_client.is_connected():
                                    for device in self.get_connector_devices(connector):
                                        self.del_device(device, False)

    def __init_and_start_regular_connector(self, _id, _type, name, configuration):
        connector = None
        if configuration is not None and self.__check_connector_configuration(configuration):

            available_connector = self.available_connectors_by_id.get(_id)

            if available_connector is None or available_connector.is_stopped():
                connector = self._implemented_connectors[_type](self, deepcopy(configuration), _type)
                connector.name = name
                self.available_connectors_by_id[_id] = connector
                self.available_connectors_by_name[name] = connector
                try:
                    report_strategy_config_connector = configuration.pop(REPORT_STRATEGY_PARAMETER, None)  # noqa
                    connector_report_strategy = ReportStrategyConfig(report_strategy_config_connector)  # noqa
                    if self._report_strategy_service is not None:
                        self._report_strategy_service.register_connector_report_strategy(name, _id, connector_report_strategy)  # noqa
                except ValueError:
                    log.info("Cannot find separated report strategy for connector %r. \
                             The main report strategy \
                             will be used as a connector report strategy.",
                             name)
                self.__update_connector_devices(connector)
                self.__cleanup_connectors()
                connector.open()
            else:
                log.debug("[%r] Connector with name %s already exists and not stopped, skipping updating it...",
                          _id, name)
                try:
                    report_strategy_config_connector = configuration.pop(REPORT_STRATEGY_PARAMETER, None)  # noqa
                    connector_report_strategy = ReportStrategyConfig(report_strategy_config_connector)  # noqa
                    if self._report_strategy_service is not None:
                        self._report_strategy_service.register_connector_report_strategy(name, _id, connector_report_strategy)  # noqa
                except ValueError:
                    log.info("Cannot find separated report strategy for connector %r. \
                             The main report strategy \
                             will be used as a connector report strategy.",
                             name)
        else:
            if configuration is not None:
                log.warning("[%r] Config incorrect for %s connector with name %s", _id, _type, name)
            else:
                log.warning("[%r] Config is empty for %s connector with name %s", _id, _type, name)

        return connector

    def __init_and_start_grpc_connector(self, _type, configuration):
        self.__grpc_connectors.update({configuration['grpc_key']: configuration})
        if _type != 'grpc':
            connector_dir_abs = "/".join(self._config_dir.split("/")[:-2])
            connector_file_name = f'{_type}_connector.py'
            connector_abs_path = f'{connector_dir_abs}/grpc_connectors/{_type}/{connector_file_name}'  # noqa
            connector_config_json = dumps({
                **configuration,
                'gateway': {
                    'host': 'localhost',
                    'port': self.__config['grpc']['serverPort']
                }
            })

            thread = Thread(target=self._run_connector,
                            args=(connector_abs_path, connector_config_json,),
                            daemon=True, name='Separated GRPC Connector')
            thread.start()

    @staticmethod
    def __check_connector_configuration(connector_configuration):
        return ("logLevel" in connector_configuration and len(connector_configuration) > 3) or \
            ("logLevel" not in connector_configuration and len(connector_configuration.keys()) >= 1)

    def _run_connector(self, connector_abs_path, connector_config_json):
        subprocess.run(['python3', connector_abs_path, connector_config_json, self._config_dir],
                       check=True,
                       universal_newlines=True)

    def check_connector_configuration_updates(self):
        configuration_changed = False
        for connector_type in self.connectors_configs:
            for connector_config in self.connectors_configs[connector_type]:
                if stat(connector_config["config_file_path"]) != connector_config["config_updated"]:
                    configuration_changed = True
                    break
            if configuration_changed:
                break
        if configuration_changed:
            self.__close_connectors()
            self._load_connectors()
            self.__connect_with_connectors()

            # Updating global self.__config['connectors'] configuration for states syncing
            for connector_type in self.connectors_configs:
                for connector_config in self.connectors_configs[connector_type]:
                    for (index, connector) in enumerate(self.__config['connectors']):
                        if connector_config['config'].get(connector['configuration']):
                            self.__config['connectors'][index]['configurationJson'] = connector_config['config'][
                                connector['configuration']]

            if self.__remote_configurator is not None:
                self.__remote_configurator.send_current_configuration()

    def send_to_storage(self, connector_name, connector_id, data: Union[dict, ConvertedData] = None):
        if data is None:
            log.error("[%r]Data is empty from connector %r!", connector_id, connector_name)
        try:
            device_valid = True
            if self.__device_filter:
                device_valid = self.__device_filter.validate_device(connector_name, data)

            if not device_valid:
                log.warning('Device %s forbidden', data['deviceName'])
                return Status.FORBIDDEN_DEVICE

            # Duplicate detector is deprecated!
            # if isinstance(data, dict):
            #     #TODO: implement data filtering for ConvertedData type
            #     filtered_data = self.__duplicate_detector.filter_data(connector_name, data)
            # else:
            #     filtered_data = data
            if isinstance(data, ConvertedData):
                if data.metadata and self.__latency_debug_mode:
                    data.add_to_metadata({SEND_TO_STORAGE_TS_PARAMETER: int(time() * 1000),
                                          CONNECTOR_PARAMETER: connector_name})
            filtration_start = time() * 1000
            if self._report_strategy_service is not None:
                self._report_strategy_service.filter_data_and_send(data, connector_name, connector_id)
            else:
                self.__converted_data_queue.put((connector_name, connector_id, data))
            filtration_end = time() * 1000
            if self.__latency_debug_mode:
                log.debug("Data filtration took %r ms", filtration_end - filtration_start)
            return Status.SUCCESS
        except Exception as e:
            log.error("Cannot put converted data!", exc_info=e)
            return Status.FAILURE

    def __send_to_storage(self):
        while not self.stopped:
            try:
                tasks = []
                collecting_start = int(monotonic() * 1000)
                batch_size = 1000
                while not self.__converted_data_queue.empty():
                    connector_name, connector_id, event = self.__converted_data_queue.get_nowait()
                    tasks.append((connector_name, connector_id, event))
                    if len(tasks) >= batch_size or int(monotonic() * 1000) - collecting_start > 500:
                        break

                if tasks:
                    for task in tasks:
                        self.__process_event(task)
                else:
                    self.stop_event.wait(0.01)
            except Exception as e:
                log.error("Error while sending data to storage!", exc_info=e)

    def __process_event(self, task):
        connector_name, connector_id, event = task
        converted_data_format = isinstance(event, ConvertedData)
        data_array = event if isinstance(event, list) else [event]
        if converted_data_format:
            if self.__latency_debug_mode:
                event.add_to_metadata({"getFromConvertedDataQueueTs": int(time() * 1000),
                                       "connector": connector_name})
            self.__send_to_storage_new_formatted_data(connector_name, connector_id, data_array)
            log.debug("Data from %s connector was sent to storage: %r", connector_name, data_array)
            current_time = int(time() * 1000)
            if self.__latency_debug_mode and event.metadata.get(SEND_TO_STORAGE_TS_PARAMETER):
                log.debug("Event was in queue for %r ms",
                          current_time - event.metadata.get(SEND_TO_STORAGE_TS_PARAMETER))
            if self.__latency_debug_mode and event.metadata.get(DATA_RETRIEVING_STARTED):
                log.debug("Data retrieving and conversion took %r ms",
                          current_time - event.metadata.get(DATA_RETRIEVING_STARTED))
        else:
            self.__send_to_storage_old_formatted_data(connector_name, connector_id, data_array)

    def __send_to_storage_new_formatted_data(self, connector_name, connector_id, data_array: List[ConvertedData]):
        max_data_size = self.get_max_payload_size_bytes()
        pack_processing_time = 0
        for data in data_array:
            if not self.__latency_debug_mode:
                data.metadata = {}
            if connector_name == self.name:
                data.device_name = "currentThingsBoardGateway"
                data.device_type = "gateway"
            else:
                if not TBUtility.validate_converted_data(data):
                    log.error("[%r] Data from %s connector is invalid.", connector_id, connector_name)
                    continue
                if data.device_name in self.__renamed_devices:
                    data.device_name = self.__renamed_devices[data.device_name]
                if self.tb_client.is_connected() and (data.device_name not in self.get_devices() or
                                                      data.device_name not in self.__connected_devices):
                    if self.available_connectors_by_id.get(connector_id) is not None:
                        self.add_device(data.device_name,
                                        {CONNECTOR_PARAMETER: self.available_connectors_by_id[connector_id]},
                                        device_type=data.device_type)
                    elif self.available_connectors_by_name.get(connector_name) is not None:
                        self.add_device(data.device_name,
                                        {CONNECTOR_PARAMETER: self.available_connectors_by_name[connector_name]},
                                        device_type=data.device_type)
                    else:
                        log.trace("Connector %s is not available, probably it was disabled, skipping data...", connector_name)
                        continue

                if not self.__connector_incoming_messages.get(connector_id):
                    self.__connector_incoming_messages[connector_id] = 0
                else:
                    self.__connector_incoming_messages[connector_id] += 1

                if hasattr(self, "__check_devices_idle") and self.__check_devices_idle:
                    self.__connected_devices[data['deviceName']]['last_receiving_data'] = time()

                adopted_data_max_entry_size = max_data_size - DEBUG_METADATA_TEMPLATE_SIZE - len(connector_name) \
                    if self.__latency_debug_mode else max_data_size
                start_splitting = int(time() * 1000)
                adopted_data: List[ConvertedData] = data.convert_to_objects_with_maximal_size(adopted_data_max_entry_size) # noqa
                end_splitting = int(time() * 1000)
                if self.__latency_debug_mode:
                    log.trace("Data splitting took %r ms, telemetry datapoints count: %r, attributes count: %r",
                              end_splitting - start_splitting,
                              data.telemetry_datapoints_count,
                              data.attributes_datapoints_count)
                if self.__latency_debug_mode and data.metadata.get("receivedTs"):
                    log.debug("Data processing before sending to storage took %r ms",
                              end_splitting - data.metadata.get("receivedTs", 0))
                for adopted_data_entry in adopted_data:
                    self.__send_data_pack_to_storage(adopted_data_entry, connector_name, connector_id)

    def __send_to_storage_old_formatted_data(self, connector_name, connector_id, data_array):
        max_data_size = self.get_max_payload_size_bytes()
        for data in data_array:
            if not connector_name == self.name:
                if 'telemetry' not in data:
                    data['telemetry'] = []
                if 'attributes' not in data:
                    data['attributes'] = []
                if not TBUtility.validate_converted_data(data):
                    log.error("[%r] Data from %s connector is invalid.", connector_id, connector_name)
                    continue
                if data.get('deviceType') is None:
                    device_name = data['deviceName']
                    data['deviceType'] = self.__get_device_type_for_device(device_name)
                if data["deviceName"] in self.__renamed_devices:
                    data["deviceName"] = self.__renamed_devices[data["deviceName"]]
                if self.tb_client.is_connected() and (data["deviceName"] not in self.get_devices() or
                                                      data["deviceName"] not in self.__connected_devices):
                    if self.available_connectors_by_id.get(connector_id) is not None:
                        self.add_device(data["deviceName"],
                                        {CONNECTOR_PARAMETER: self.available_connectors_by_id[connector_id]},
                                        device_type=data["deviceType"])
                    elif self.available_connectors_by_name.get(connector_name) is not None:
                        self.add_device(data["deviceName"],
                                        {CONNECTOR_PARAMETER: self.available_connectors_by_name[connector_name]},
                                        device_type=data["deviceType"])
                    else:
                        log.error("Connector %s is not available!", connector_name)

                if not self.__connector_incoming_messages.get(connector_id):
                    self.__connector_incoming_messages[connector_id] = 0
                else:
                    self.__connector_incoming_messages[connector_id] += 1
            else:
                data["deviceName"] = "currentThingsBoardGateway"
                data['deviceType'] = "gateway"

            if hasattr(self, "__check_devices_idle") and self.__check_devices_idle:
                self.__connected_devices[data['deviceName']]['last_receiving_data'] = time()

            data = self.__convert_telemetry_to_ts(data)
            if TBUtility.get_data_size(data) >= max_data_size:
                # Data is too large, so we will attempt to send in pieces
                adopted_data = {"deviceName": data['deviceName'],
                                "deviceType": data['deviceType'],
                                "attributes": {},
                                "telemetry": []}
                empty_adopted_data_size = TBUtility.get_data_size(adopted_data)
                adopted_data_size = empty_adopted_data_size

                # First, loop through the attributes
                for attribute in data['attributes']:
                    adopted_data['attributes'].update(attribute)
                    adopted_data_size += TBUtility.get_data_size(attribute)
                    if adopted_data_size >= max_data_size:
                        # We have surpassed the max_data_size, so send what we have and clear attributes
                        self.__send_data_pack_to_storage(adopted_data, connector_name, connector_id)
                        adopted_data['attributes'] = {}
                        adopted_data_size = empty_adopted_data_size

                # Now, loop through telemetry. Possibly have some unsent attributes that have been adopted.
                telemetry = data['telemetry'] if isinstance(data['telemetry'], list) else [
                    data['telemetry']]
                ts_to_index = {}
                for ts_kv_list in telemetry:
                    ts = ts_kv_list['ts']
                    for kv in ts_kv_list['values']:
                        if ts in ts_to_index:
                            kv_data = {kv: ts_kv_list['values'][kv]}
                            adopted_data['telemetry'][ts_to_index[ts]]['values'].update(kv_data)
                        else:
                            kv_data = {'ts': ts, 'values': {kv: ts_kv_list['values'][kv]}}
                            adopted_data['telemetry'].append(kv_data)
                            ts_to_index[ts] = len(adopted_data['telemetry']) - 1

                        adopted_data_size += TBUtility.get_data_size(kv_data)
                        if adopted_data_size >= max_data_size:
                            # we have surpassed the max_data_size,
                            # so send what we have and clear attributes and telemetry
                            self.__send_data_pack_to_storage(adopted_data, connector_name, connector_id)
                            adopted_data['telemetry'] = []
                            adopted_data['attributes'] = {}
                            adopted_data_size = empty_adopted_data_size
                            ts_to_index = {}

                # It is possible that we get here and have some telemetry or attributes not yet sent,
                # so check for that.
                if len(adopted_data['telemetry']) > 0 or len(adopted_data['attributes']) > 0:
                    self.__send_data_pack_to_storage(adopted_data, connector_name, connector_id)
                    # technically unnecessary to clear here, but leaving for consistency.
                    adopted_data['telemetry'] = []
                    adopted_data['attributes'] = {}
            else:
                self.__send_data_pack_to_storage(data, connector_name, connector_id)

    def __get_device_type_for_device(self, device_name):
        if self.__connected_devices.get(device_name) is not None:
            return self.__connected_devices[device_name]['device_type']
        elif self.__saved_devices.get(device_name) is not None:
            return self.__saved_devices[device_name]['device_type']
        elif self.__renamed_devices.get(device_name) is not None:
            return self.__get_device_type_for_device(self.__renamed_devices[device_name])
        else:
            return "default"

    @staticmethod
    def __convert_telemetry_to_ts(data):
        telemetry = {}
        telemetry_with_ts = []
        for item in data["telemetry"]:
            if item.get("ts") is None:
                telemetry.update(item)
            else:
                if isinstance(item['ts'], int):
                    telemetry_with_ts.append({"ts": item["ts"], "values": item["values"]})
                else:
                    log.warning('Data has invalid TS (timestamp) format! Using generated TS instead.')
                    telemetry_with_ts.append({"ts": int(time() * 1000), "values": item["values"]})

        if telemetry_with_ts:
            data["telemetry"] = telemetry_with_ts
        elif len(data['telemetry']) > 0:
            data["telemetry"] = {"ts": int(time() * 1000), "values": telemetry}
        return data

    @CollectStorageEventsStatistics('storageMsgPushed')
    def __send_data_pack_to_storage(self, data, connector_name, connector_id=None):
        if isinstance(data, ConvertedData):
            if self.__latency_debug_mode:
                data.add_to_metadata({"putToStorageTs": int(time() * 1000)})
            json_data = dumps(data.to_dict(self.__latency_debug_mode), separators=(',', ':'), skipkeys=True)
        else:
            json_data = dumps(data, separators=(',', ':'), skipkeys=True)
        save_result = self._event_storage.put(json_data)
        tries = 4
        current_try = 0
        while not save_result and current_try < tries:
            sleep(0.1)
            save_result = self._event_storage.put(json_data)
            current_try += 1
        if not save_result:
            log.error('%rData from the device "%s" cannot be saved, connector name is %s.',
                      "[" + connector_id + "] " if connector_id is not None else "",
                      data.device_name if isinstance(data, ConvertedData) else data["deviceName"], connector_name)

    # def check_size(self, devices_data_in_event_pack, current_data_pack_size, item_size):
    #
    #     if current_data_pack_size + item_size >= self.get_max_payload_size_bytes() - max(100, self.get_max_payload_size_bytes()/10): # noqa
    #         current_data_pack_size = TBUtility.get_data_size(devices_data_in_event_pack)
    #     else:
    #         current_data_pack_size += item_size
    #
    #     if current_data_pack_size >= self.get_max_payload_size_bytes():
    #         self.__send_data(devices_data_in_event_pack)
    #         for device in devices_data_in_event_pack:
    #             devices_data_in_event_pack[device]["telemetry"] = []
    #             devices_data_in_event_pack[device]["attributes"] = {}
    #         current_data_pack_size = 0
    #     return current_data_pack_size

    def __read_data_from_storage(self):
        devices_data_in_event_pack = {}
        global log
        log.debug("Send data Thread has been started successfully.")
        log.debug("Maximal size of the client message queue is: %r",
                  self.tb_client.client._client._max_queued_messages) # noqa pylint: disable=protected-access
        current_event_pack_data_size = 0
        logger_get_time = 0

        while not self.stopped:
            try:
                if monotonic() - logger_get_time > 60:
                    log = logging.getLogger('service')
                    logger_get_time = monotonic()
                if self.tb_client.is_connected():
                    events = []

                    if self.__remote_configurator is None or not self.__remote_configurator.in_process:
                        events = self._event_storage.get_event_pack()

                    if events:
                        events_len = len(events)
                        StatisticsService.add_count('storageMsgPulled', count=events_len)

                        # telemetry_dp_count and attribute_dp_count using only for statistics
                        telemetry_dp_count = 0
                        attribute_dp_count = 0

                        if self.__latency_debug_mode and events_len > 100:
                            log.debug("Retrieved %r events from the storage.", events_len)
                        start_pack_processing = time()
                        for event in events:
                            try:
                                current_event = loads(event)
                            except Exception as e:
                                log.error("Error while processing event from the storage, it will be skipped.",
                                          exc_info=e)
                                continue

                            if not devices_data_in_event_pack.get(current_event["deviceName"]): # noqa
                                devices_data_in_event_pack[current_event["deviceName"]] = {"telemetry": [],
                                                                                           "attributes": {}}
                            # start_processing_telemetry_in_event = time()
                            has_metadata = False
                            if current_event.get('metadata'):
                                has_metadata = True
                            if current_event.get("telemetry"):
                                if isinstance(current_event["telemetry"], list):
                                    for item in current_event["telemetry"]:
                                        if has_metadata and item.get('ts'):
                                            item.update({'metadata': current_event.get('metadata')})
                                        # current_event_pack_data_size = self.check_size(devices_data_in_event_pack,
                                        #                                                current_event_pack_data_size,
                                        #                                                TBUtility.get_data_size(item))
                                        devices_data_in_event_pack[current_event["deviceName"]]["telemetry"].append(item) # noqa
                                        telemetry_dp_count += len(item.get('values', []))
                                else:
                                    if has_metadata and current_event["telemetry"].get('ts'):
                                        current_event["telemetry"].update({'metadata': current_event.get('metadata')})
                                    # current_event_pack_data_size = self.check_size(devices_data_in_event_pack,
                                    #                                                current_event_pack_data_size,
                                    #                                                TBUtility.get_data_size(current_event["telemetry"])) # noqa
                                    devices_data_in_event_pack[current_event["deviceName"]]["telemetry"].append(current_event["telemetry"]) # noqa
                                    telemetry_dp_count += len(current_event["telemetry"].get('values', []))
                            # log.debug("Processing telemetry in event took %r seconds.", time() - start_processing_telemetry_in_event) # noqa
                            # start_processing_attributes_in_event = time()
                            if current_event.get("attributes"):
                                if isinstance(current_event["attributes"], list):
                                    for item in current_event["attributes"]:
                                        # current_event_pack_data_size = self.check_size(devices_data_in_event_pack,
                                        #                                                current_event_pack_data_size,
                                        #                                                TBUtility.get_data_size(item))
                                        devices_data_in_event_pack[current_event["deviceName"]]["attributes"].update(item.items()) # noqa
                                        attribute_dp_count += 1
                                else:
                                    # current_event_pack_data_size = self.check_size(devices_data_in_event_pack,
                                    #                                                current_event_pack_data_size,
                                    #                                                TBUtility.get_data_size(current_event["attributes"])) # noqa
                                    devices_data_in_event_pack[current_event["deviceName"]]["attributes"].update(current_event["attributes"].items()) # noqa
                                    attribute_dp_count += 1

                            # log.debug("Processing attributes in event took %r seconds.", time() - start_processing_attributes_in_event) # noqa
                        log.debug("Telemetry dp count: %r and attributes dp count: %r. Counting took: %r milliseconds.",  # noqa
                                  telemetry_dp_count, attribute_dp_count, int((time() - start_pack_processing)*1000))  # noqa
                        if devices_data_in_event_pack:
                            if not self.tb_client.is_connected():
                                continue
                            while self.__rpc_reply_sent:
                                self.stop_event.wait(0.01)
                            if self.__latency_debug_mode and events_len > 100:
                                pack_processing_time = int((time() - start_pack_processing) * 1000)
                                average_event_processing_time = (pack_processing_time / events_len)
                                if average_event_processing_time < 1.0:
                                    average_event_processing_time_str = f"{average_event_processing_time * 1000:.2f} microseconds." # noqa
                                else:
                                    average_event_processing_time_str = f"{average_event_processing_time:.2f} milliseconds." # noqa
                                log.debug("Sending data to ThingsBoard, pack size %i processing took %i ,milliseconds. Average event processing time is %s",  # noqa
                                          events_len,
                                          pack_processing_time,
                                          average_event_processing_time_str) # noqa

                            self.__send_data(devices_data_in_event_pack) # noqa
                            current_event_pack_data_size = 0

                        if self.tb_client.is_connected() and (
                                self.__remote_configurator is None or not self.__remote_configurator.in_process):

                            success = self.__handle_published_events()

                            if success and self.tb_client.is_connected():
                                self._event_storage.event_pack_processing_done()
                                del devices_data_in_event_pack
                                devices_data_in_event_pack = {}
                                StatisticsService.add_count('platformTsProduced', count=telemetry_dp_count)
                                StatisticsService.add_count('platformAttrProduced', count=attribute_dp_count)
                                StatisticsService.add_count('platformMsgPushed', count=len(events))
                        else:
                            continue
                    else:
                        self.stop_event.wait(self.__min_pack_send_delay_ms)
                else:
                    self.stop_event.wait(1)
            except Exception as e:
                log.error("Error while sending data to ThingsBoard, it will be resent.", exc_info=e)
                self.stop_event.wait(1)
        log.info("Send data Thread has been stopped successfully.")

    def __handle_published_events(self):
        events = []

        while not self._published_events.empty() and not self.stopped:
            try:
                events.append(self._published_events.get_nowait())
            except Empty:
                break

        if not events:
            return False

        futures = []
        try:
            if self.tb_client.is_connected() and (self.__remote_configurator is None or
                                                  not self.__remote_configurator.in_process):
                qos = self.tb_client.client.quality_of_service
                if qos == 1:
                    futures = list(self.__messages_confirmation_executor.map(self.__process_published_event, events))

            event_num = 0
            for success in futures:
                event_num += 1
                if event_num % 100 == 0:
                    log.debug("Confirming %i event sent to ThingsBoard", event_num)
                if not success:
                    return False

            return True
        except Exception:  # noqa
            log.debug("Error while sending data to ThingsBoard, it will be resent.")
            return False

    @staticmethod
    def __process_published_event(event):
        try:
            return event.get() == event.TB_ERR_SUCCESS
        except RuntimeError as e:
            log.error("Error while sending data to ThingsBoard, it will be resent.", exc_info=e)
            return False
        except Exception as e:
            log.error("Error while sending data to ThingsBoard, it will be resent.", exc_info=e)
            return False

    @CollectAllSentTBBytesStatistics(start_stat_type='allBytesSentToTB')
    def __send_data(self, devices_data_in_event_pack):
        try:
            for device in devices_data_in_event_pack:
                final_device_name = device if self.__renamed_devices.get(device) is None else self.__renamed_devices[
                    device]

                if devices_data_in_event_pack[device].get("attributes"):
                    if device == self.name or device == "currentThingsBoardGateway":
                        self._published_events.put(
                            self.send_attributes(devices_data_in_event_pack[device]["attributes"]))
                    else:
                        self._published_events.put(self.gw_send_attributes(final_device_name,
                                                                           devices_data_in_event_pack[
                                                                               device]["attributes"]))
                if devices_data_in_event_pack[device].get("telemetry"):
                    if device == self.name or device == "currentThingsBoardGateway":
                        self._published_events.put(
                            self.send_telemetry(devices_data_in_event_pack[device]["telemetry"]))
                    else:
                        self._published_events.put(self.gw_send_telemetry(final_device_name,
                                                                          devices_data_in_event_pack[
                                                                              device]["telemetry"]))
                devices_data_in_event_pack[device] = {"telemetry": [], "attributes": {}}
        except Exception as e:
            log.error("Error while sending data to ThingsBoard, it will be resent.", exc_info=e)

    @CountMessage('msgsReceivedFromPlatform')
    def _rpc_request_handler(self, request_id, content):
        try:
            if not isinstance(request_id, int) and 'data' in content:
                request_id = content['data'].get('id')
            device = content.get("device")
            if device is not None:
                self.__rpc_to_devices_queue.put((request_id, content, monotonic()))
            else:
                try:
                    method_split = content["method"].split('_')
                    module = None
                    if len(method_split) > 0:
                        module = method_split[0]
                    if module is not None:
                        result = None
                        if self.connectors_configs.get(module):
                            log.debug("Connector \"%s\" for RPC request \"%s\" found", module, content["method"])
                            for connector_name in self.available_connectors_by_name:
                                if self.available_connectors_by_name[connector_name]._connector_type == module: # noqa pylint: disable=protected-access
                                    log.debug("Sending command RPC %s to connector %s", content["method"],
                                              connector_name)
                                    content['id'] = request_id
                                    result = self.available_connectors_by_name[connector_name].server_side_rpc_handler(content) # noqa E501
                        elif module == 'gateway' or (self.__remote_shell and module in self.__remote_shell.shell_commands): # noqa
                            result = self.__rpc_gateway_processing(request_id, content)
                        else:
                            log.error("Connector \"%s\" not found", module)
                            result = {"error": "%s - connector not found in available connectors." % module,
                                      "code": 404}
                        if result is None:
                            self.send_rpc_reply(None, request_id, success_sent=False)
                        elif isinstance(result, dict) and "qos" in result:
                            self.send_rpc_reply(None, request_id,
                                                dumps({k: v for k, v in result.items() if k != "qos"}),
                                                quality_of_service=result["qos"])
                        else:
                            self.send_rpc_reply(None, request_id, dumps(result))
                except Exception as e:
                    self.send_rpc_reply(None, request_id, "{\"error\":\"%s\", \"code\": 500}" % str(e))
                    log.error("Error while processing RPC request to service", exc_info=e)
        except Exception as e:
            log.error("Error while processing RPC request", exc_info=e)

    def __rpc_to_devices_processing(self):
        while not self.stopped:
            try:
                request_id, content, received_time = self.__rpc_to_devices_queue.get_nowait()
                timeout = content.get("params", {}).get("timeout", self.DEFAULT_TIMEOUT)
                if monotonic() - received_time > timeout:
                    log.error("RPC request %s timeout", request_id)
                    self.send_rpc_reply(content["device"], request_id, "{\"error\":\"Request timeout\", \"code\": 408}")
                    continue
                device = content.get("device")
                original_name = TBUtility.get_dict_key_by_value(self.__renamed_devices, device)
                if original_name is not None:
                    content['device'] = original_name
                    device = original_name
                if device in self.get_devices():
                    connector = self.get_devices()[content['device']].get(CONNECTOR_PARAMETER)
                    if connector is not None:
                        content['id'] = request_id
                        result = connector.server_side_rpc_handler(content)
                        if result is not None and isinstance(result, dict) and 'error' in result:
                            self.send_rpc_reply(device, request_id, dumps(result), success_sent=False)
                    else:
                        log.error("Received RPC request but connector for the device %s not found. Request data: \n %s",
                                  content["device"],
                                  dumps(content))
                else:
                    self.__rpc_to_devices_queue.put((request_id, content, received_time))
            except (TimeoutError, Empty):
                self.stop_event.wait(.1)

    def __rpc_gateway_processing(self, request_id, content):
        log.info("Received RPC request to the gateway, id: %s, method: %s", str(request_id), content["method"])
        arguments = content.get('params', {})
        method_to_call = content["method"].replace("gateway_", "")

        if self.__remote_shell is not None:
            method_function = self.__remote_shell.shell_commands.get(method_to_call,
                                                                     self.__gateway_rpc_methods.get(method_to_call))
        else:
            method_function = self.__gateway_rpc_methods.get(method_to_call)

        if method_function is None and method_to_call in self.__rpc_scheduled_methods_functions:
            seconds_to_restart = arguments * 1000 if arguments and arguments != '{}' else 1000
            seconds_to_restart = max(seconds_to_restart, 1000)
            self.__scheduled_rpc_calls.append([time() * 1000 + seconds_to_restart,
                                               self.__rpc_scheduled_methods_functions[method_to_call]])
            log.info("Gateway %s scheduled in %i seconds", method_to_call, seconds_to_restart / 1000)
            result = {"success": True}
        elif method_function is None:
            log.error("RPC method %s - Not found", content["method"])
            return {"error": "Method not found", "code": 404}
        elif isinstance(arguments, list):
            result = method_function(*arguments)
        elif arguments == '{}' or arguments is None:
            result = method_function()
        else:
            result = method_function(arguments)

        return result

    @staticmethod
    def __rpc_ping(*args):
        log.debug("Ping RPC request received with arguments %s", args)
        return {"code": 200, "resp": "pong"}

    def __rpc_devices(self, *args):
        log.debug("Devices RPC request received with arguments %s", args)
        data_to_send = {}
        for device in self.__connected_devices:
            if self.__connected_devices[device][CONNECTOR_PARAMETER] is not None:
                data_to_send[device] = self.__connected_devices[device][CONNECTOR_PARAMETER].get_name()
        return {"code": 200, "resp": data_to_send}

    def __rpc_update(self, *args):
        log.debug("Update RPC request received with arguments %s", args)
        try:
            result = {"resp": self.__updater.update(),
                      "code": 200,
                      }
        except Exception as e:
            result = {"error": str(e),
                      "code": 500
                      }
        return result

    def __rpc_version(self, *args):
        log.debug("Version RPC request received with arguments %s", args)
        try:
            result = {"resp": self.__updater.get_version(), "code": 200}
        except Exception as e:
            result = {"error": str(e), "code": 500}
        return result

    def is_rpc_in_progress(self, topic):
        return topic in self.__rpc_requests_in_progress

    def rpc_with_reply_processing(self, topic, content):
        req_id = self.__rpc_requests_in_progress[topic][0]["data"]["id"]
        device = self.__rpc_requests_in_progress[topic][0]["device"]
        log.info("Outgoing RPC. Device: %s, ID: %d", device, req_id)
        self.send_rpc_reply(device, req_id, content)

    @CollectRPCReplyStatistics(start_stat_type='allBytesSentToTB')
    @CountMessage('msgsSentToPlatform')
    def send_rpc_reply(self, device=None, req_id=None, content=None, success_sent=None, wait_for_publish=None,
                       quality_of_service=0, to_connector_rpc=False):
        self.__rpc_processing_queue.put((device, req_id, content, success_sent,
                                         wait_for_publish, quality_of_service, to_connector_rpc))

    def __send_rpc_reply_processing(self):
        while not self.stopped:
            try:
                args = self.__rpc_processing_queue.get(timeout=1)
                self.__send_rpc_reply(*args)
            except (TimeoutError, Empty):
                self.stop_event.wait(0.05)

    def __send_rpc_reply(self, device=None, req_id=None, content=None, success_sent=None, wait_for_publish=None,
                         quality_of_service=0, to_connector_rpc=False):
        try:
            if device in self.__renamed_devices:
                device = self.__renamed_devices[device]
            self.__rpc_reply_sent = True
            rpc_response = {"success": False}
            if success_sent is not None:
                if success_sent:
                    rpc_response["success"] = True
            if isinstance(content, str):
                try:
                    content = loads(content)
                except Exception:
                    pass
            if content is not None and isinstance(content, dict) and \
                    ('success' in content or 'error' in content or 'response' in content or 'result' in content):
                rpc_response = content
                if success_sent is not None:
                    rpc_response["success"] = success_sent

            if 'result' in rpc_response:  # For get/set service RPCs
                rpc_response = rpc_response['result']

            if device is not None and success_sent is not None and not to_connector_rpc:
                self.tb_client.client.gw_send_rpc_reply(device, req_id, dumps(rpc_response),
                                                        quality_of_service=quality_of_service)
            elif device is not None and req_id is not None and content is not None and not to_connector_rpc:
                self.tb_client.client.gw_send_rpc_reply(device, req_id, content, quality_of_service=quality_of_service)
            elif (device is None or to_connector_rpc) and success_sent is not None:
                self.tb_client.client.send_rpc_reply(req_id, dumps(rpc_response), quality_of_service=quality_of_service,
                                                     wait_for_publish=wait_for_publish)
            elif (device is None and content is not None) or to_connector_rpc:
                self.tb_client.client.send_rpc_reply(req_id, content, quality_of_service=quality_of_service,
                                                     wait_for_publish=wait_for_publish)
            self.__rpc_reply_sent = False
        except Exception as e:
            log.error("Error while sending RPC reply", exc_info=e)
            self.__rpc_reply_sent = False

    def register_rpc_request_timeout(self, content, timeout, topic, cancel_method):
        # Put request in outgoing RPC queue. It will be eventually dispatched.
        self.__rpc_register_queue.put({"topic": topic, "data": (content, timeout, cancel_method)}, False)

    def cancel_rpc_request(self, rpc_request):
        content = self.__rpc_requests_in_progress[rpc_request][0]
        try:
            self.send_rpc_reply(device=content["device"], req_id=content["data"]["id"], success_sent=False)
        except Exception as e:
            log.error("Error while canceling RPC request", exc_info=e)

    @CountMessage('msgsReceivedFromPlatform')
    def _attribute_update_callback(self, content, *args):
        if not content:
            log.error("Attribute request received with empty content")
            return
        log.debug("Attribute request received with content: \"%s\"", content)
        log.debug(args)
        device_name = content.get('device')
        if device_name is not None and ('value' in content or 'values' in content or 'data' in content):
            if content.get('id') is not None:
                if content.get('value') is not None \
                        and len(args) > 1 and isinstance(args[-1], list) and len(args[-1]) == 1:
                    content = {'data': {args[1][0]: content['value']}, 'device': device_name}
                elif content.get('values') is not None:
                    content = {'data': content['values'], 'device': device_name}
                else:
                    log.error("Unexpected format of attribute response received: \"%s\"", content)
            try:
                target_device_name = TBUtility.get_dict_key_by_value(self.__renamed_devices, device_name)
                if target_device_name is None:
                    target_device_name = device_name
                if self.__sync_devices_shared_attributes_on_connect:
                    if target_device_name in self.__devices_shared_attributes:
                        self.__devices_shared_attributes[target_device_name].update(content['data'])  # noqa
                    else:
                        self.__devices_shared_attributes[target_device_name] = content['data']
                if self.__connected_devices.get(target_device_name) is not None:
                    device_connector = self.__connected_devices[target_device_name][CONNECTOR_PARAMETER]
                    content['device'] = target_device_name
                    device_connector.on_attributes_update(content)
            except Exception as e:
                log.error("Error while processing attributes update", exc_info=e)
        else:
            self._attributes_parse(content)

    def __form_statistics(self):
        summary_messages = {"eventsProduced": 0, "eventsSent": 0}
        for connector in self.available_connectors_by_name:
            connector_camel_case = connector.replace(' ', '')
            telemetry = {
                (connector_camel_case + ' EventsProduced').replace(' ', ''):
                    self.available_connectors_by_name[connector].statistics.get('MessagesReceived', 0), # noqa
                (connector_camel_case + ' EventsSent').replace(' ', ''):
                    self.available_connectors_by_name[connector].statistics.get('MessagesSent', 0) # noqa
            }
            summary_messages['eventsProduced'] += telemetry[
                str(connector_camel_case + ' EventsProduced').replace(' ', '')]
            summary_messages['eventsSent'] += telemetry[
                str(connector_camel_case + ' EventsSent').replace(' ', '')]
            summary_messages.update(telemetry)
        return summary_messages

    def add_device_async(self, data):
        if data['deviceName'] not in self.__saved_devices:
            self.__async_device_actions_queue.put((DeviceActions.CONNECT, data))
            return Status.SUCCESS
        else:
            return Status.FAILURE

    def add_device(self, device_name, content, device_type=None):
        if self.tb_client is None or not self.tb_client.is_connected():
            self.__devices_shared_attributes = {}
            return False

        device_type = device_type if device_type is not None else 'default'

        if device_name in self.__renamed_devices:
            if self.__sync_devices_shared_attributes_on_connect and hasattr(content['connector'],
                                                                            'get_device_shared_attributes_keys'):
                self.__sync_device_shared_attrs_queue.put((self.__renamed_devices[device_name], content['connector']))
            self.__disconnected_devices.pop(device_name, None)
            self.__save_persistent_devices()
            return True

        if (device_name in self.__connected_devices or TBUtility.get_dict_key_by_value(self.__renamed_devices, device_name) is not None):
            if self.__sync_devices_shared_attributes_on_connect and hasattr(content['connector'],'get_device_shared_attributes_keys'):
                self.__sync_device_shared_attrs_queue.put((device_name, content['connector']))

            return True


        self.__connected_devices[device_name] = {**content, DEVICE_TYPE_PARAMETER: device_type}
        self.__saved_devices[device_name] = {**content, DEVICE_TYPE_PARAMETER: device_type}
        self.__save_persistent_devices()
        self.tb_client.client.gw_connect_device(device_name, device_type).get()
        if device_name in self.__saved_devices:
            if content.get(CONNECTOR_PARAMETER) is not None:
                connector_type = content['connector'].get_type()
                connector_name = content['connector'].get_name()
                try:
                    if (self.__added_devices.get(device_name) is None
                        or (self.__added_devices[device_name]['device_details']['connectorType'] != connector_type
                            or self.__added_devices[device_name]['device_details']['connectorName'] != connector_name)):
                        device_details = {
                            'connectorType': connector_type,
                            'connectorName': connector_name
                        }
                        self.__added_devices[device_name] = {"device_details": device_details,
                                                             "last_send_ts": monotonic()}
                        self.gw_send_attributes(device_name, device_details)
                except Exception as e:
                    global log
                    log.error("Error on sending device details about the device %s", device_name, exc_info=e)
                    return False

        if self.__sync_devices_shared_attributes_on_connect and hasattr(content['connector'],
                                                                        'get_device_shared_attributes_keys'):
            self.__sync_device_shared_attrs_queue.put((device_name, content['connector']))
        return True

    def __sync_device_shared_attrs_loop(self):
        while not self.stopped:
            try:
                device_name, connector = self.__sync_device_shared_attrs_queue.get_nowait()
                self.__process_sync_device_shared_attrs(device_name, connector)
            except Empty:
                self.stop_event.wait(0.1)

    def __process_sync_device_shared_attrs(self, device_name, connector):
        target_device_name = TBUtility.get_dict_key_by_value(self.__renamed_devices, device_name)
        if target_device_name is None:
            target_device_name = device_name
        shared_attributes = connector.get_device_shared_attributes_keys(target_device_name)
        if device_name in self.__devices_shared_attributes:
            device_shared_attrs = self.__devices_shared_attributes.get(device_name)
            shared_attributes_request = {
                'device': device_name,
                'data': device_shared_attrs
            }
            # TODO: request shared attributes on init for all configured devices simultaneously to synchronize shared attributes  # noqa
            connector.on_attributes_update(shared_attributes_request)
        else:
            if shared_attributes:
                if shared_attributes == '*':
                    shared_attributes = []
                self.tb_client.client.gw_request_shared_attributes(device_name,
                                                                   shared_attributes,
                                                                   (self._attribute_update_callback, shared_attributes))

    def update_device(self, device_name, event, content: Connector):
        should_save = False
        if self.__connected_devices.get(device_name) is None:
            return
        if (event == 'connector' and (self.__connected_devices[device_name].get(event) != content
                                      or id(content) != id(self.__connected_devices[device_name][event]))):
            should_save = True
        self.__connected_devices[device_name][event] = content
        if should_save:
            self.__save_persistent_devices()
            info_to_send = {
                DatapointKey("connectorName", ReportStrategyConfig({"type": ReportStrategy.ON_RECEIVED.name})):
                    content.get_name()
            }
            if device_name in self.__connected_devices:  # TODO: check for possible race condition
                self.send_to_storage(connector_name=content.get_name(),
                                     connector_id=content.get_id(),
                                     data={"deviceName": device_name,
                                           "deviceType": self.__connected_devices[device_name][DEVICE_TYPE_PARAMETER],
                                           "attributes": [info_to_send]})

    def del_device_async(self, data):
        if data['deviceName'] in self.__saved_devices:
            self.__async_device_actions_queue.put((DeviceActions.DISCONNECT, data))
            return Status.SUCCESS
        else:
            return Status.FAILURE

    def del_device(self, device_name, remove_device=True):
        device = self.__connected_devices.pop(device_name, None)
        if device is None:
            device = self.__disconnected_devices.pop(device_name, None)
        if device_name is not None:
            try:
                self.tb_client.client.gw_disconnect_device(device_name)
            except Exception as e:
                log.error("Error on disconnecting device %s", device_name, exc_info=e)
            if device_name in self.__renamed_devices:
                self.__disconnected_devices[device_name] = device
            self.__saved_devices.pop(device_name, None)
            self.__added_devices.pop(device_name, None)
            self.__save_persistent_devices()
        if remove_device:
            if device_name in self.__devices_shared_attributes:
                self.__devices_shared_attributes.pop(device_name, None)

    def get_report_strategy_service(self):
        return self._report_strategy_service

    def get_devices(self, connector_id: str = None) -> dict[str, dict]:
        if connector_id is None:
            result = self.__connected_devices
        else:
            result = {
                name: info[DEVICE_TYPE_PARAMETER]
                for name, info in self.__connected_devices.items()
                if info.get(CONNECTOR_PARAMETER) is not None
                   and info[CONNECTOR_PARAMETER].get_id() == connector_id
            }

        return result


    def __process_async_device_actions(self):
        while not self.stopped:
            if not self.__async_device_actions_queue.empty():
                action, data = self.__async_device_actions_queue.get()
                if action == DeviceActions.CONNECT:
                    self.add_device(data['deviceName'],
                                    {CONNECTOR_PARAMETER: self.available_connectors_by_name[data['name']]},
                                    data.get('deviceType'))
                elif action == DeviceActions.DISCONNECT:
                    self.del_device(data['deviceName'])
            else:
                self.stop_event.wait(0.2)

    def __load_persistent_connector_keys(self):
        persistent_keys = {}
        if PERSISTENT_GRPC_CONNECTORS_KEY_FILENAME in listdir(self._config_dir) and \
                path.getsize(self._config_dir + PERSISTENT_GRPC_CONNECTORS_KEY_FILENAME) > 0:
            try:
                persistent_keys = load_file(self._config_dir + PERSISTENT_GRPC_CONNECTORS_KEY_FILENAME)
            except Exception as e:
                log.error("Error while loading persistent keys from file with error: %s", e, exc_info=e)
            log.debug("Loaded keys: %s", persistent_keys)
        else:
            log.debug("Persistent keys file not found")
        return persistent_keys

    def __save_persistent_keys(self, persistent_keys):
        try:
            with open(self._config_dir + PERSISTENT_GRPC_CONNECTORS_KEY_FILENAME, 'w') as persistent_keys_file:
                persistent_keys_file.write(dumps(persistent_keys, indent=2, sort_keys=True))
        except Exception as e:
            log.error("Error while saving persistent keys to file with error: %s", e, exc_info=e)

    def __load_persistent_devices(self):
        loaded_connected_devices = None
        if CONNECTED_DEVICES_FILENAME in listdir(self._config_dir) and \
                path.getsize(self._config_dir + CONNECTED_DEVICES_FILENAME) > 0:
            try:
                loaded_connected_devices = load_file(self._config_dir + CONNECTED_DEVICES_FILENAME)
            except Exception as e:
                log.error("Error while loading connected devices from file with error: %s", e)
        else:
            open(self._config_dir + CONNECTED_DEVICES_FILENAME, 'w').close()

        if loaded_connected_devices is not None:
            log.debug("Loaded devices:\n %s", loaded_connected_devices)
            for device_name in loaded_connected_devices:
                try:
                    loaded_connected_device = loaded_connected_devices[device_name]
                    if isinstance(loaded_connected_device, str):
                        open(self._config_dir + CONNECTED_DEVICES_FILENAME, 'w').close()
                        log.debug("Old connected_devices file, new file will be created")
                        return
                    device_data_to_save = {}
                    if isinstance(loaded_connected_device, list) \
                            and self.available_connectors_by_name.get(loaded_connected_device[0]):
                        device_data_to_save = {
                            CONNECTOR_PARAMETER: self.available_connectors_by_name[loaded_connected_device[0]], # noqa
                            DEVICE_TYPE_PARAMETER: loaded_connected_device[1]}
                        if len(loaded_connected_device) > 2 and device_name not in self.__renamed_devices:
                            new_device_name = loaded_connected_device[2]
                            self.__renamed_devices[device_name] = new_device_name
                    elif isinstance(loaded_connected_device, dict):
                        device_connector_id = loaded_connected_device[CONNECTOR_ID_PARAMETER]
                        connector = self.available_connectors_by_id.get(device_connector_id)
                        if connector is None:
                            log.warning("Connector with id %s not found, trying to use connector by name!",
                                        device_connector_id)
                            connector = self.available_connectors_by_name.get(
                                loaded_connected_device[CONNECTOR_NAME_PARAMETER])
                        if loaded_connected_device.get(RENAMING_PARAMETER) is not None:
                            new_device_name = loaded_connected_device[RENAMING_PARAMETER]
                            self.__renamed_devices[device_name] = new_device_name

                            self.__disconnected_devices[device_name] = loaded_connected_device
                        if connector is None:
                            log.debug("Connector with name %s not found! probably it is disabled, device %s will be "
                                        "removed from the saved devices",
                                      loaded_connected_device[CONNECTOR_NAME_PARAMETER], device_name)
                            continue
                        device_data_to_save = {
                            CONNECTOR_PARAMETER: connector,
                            DEVICE_TYPE_PARAMETER: loaded_connected_device[DEVICE_TYPE_PARAMETER]
                        }
                    self.__connected_devices[device_name] = device_data_to_save
                    for device in list(self.__connected_devices.keys()):
                        if device in self.__connected_devices:
                            self.add_device(device, self.__connected_devices[device], self.__connected_devices[device][
                                DEVICE_TYPE_PARAMETER])
                    self.__saved_devices[device_name] = device_data_to_save

                except Exception as e:
                    log.error("Error while loading connected devices from file with error: %s", e, exc_info=e)
                    continue
        else:
            log.debug("No device found in connected device file.")
            self.__connected_devices = {} if self.__connected_devices is None else self.__connected_devices

    def __process_connected_devices(self, data_to_save: dict) -> dict:
        for device, info in self.__connected_devices.items():
            connector = info.get(CONNECTOR_PARAMETER)
            if connector is None:
                continue
            data_to_save[device] = {
                CONNECTOR_NAME_PARAMETER: connector.get_name(),
                DEVICE_TYPE_PARAMETER: info[DEVICE_TYPE_PARAMETER],
                CONNECTOR_ID_PARAMETER: connector.get_id(),
                RENAMING_PARAMETER: self.__renamed_devices.get(device),
                DISCONNECTED_PARAMETER: False
            }
        return data_to_save

    def __process_disconnected_devices(self, data_to_save: dict) -> dict:
        for device, info in self.__disconnected_devices.items():
            connector = info.get(CONNECTOR_PARAMETER)
            if connector is not None:
                name = connector.get_name()
                cid = connector.get_id()
            else:
                name = info[CONNECTOR_NAME_PARAMETER]
                cid = info[CONNECTOR_ID_PARAMETER]

            data_to_save[device] = {
                CONNECTOR_NAME_PARAMETER: name,
                DEVICE_TYPE_PARAMETER: info[DEVICE_TYPE_PARAMETER],
                CONNECTOR_ID_PARAMETER: cid,
                RENAMING_PARAMETER: self.__renamed_devices.get(device),
                DISCONNECTED_PARAMETER: True
            }
        return data_to_save

    def __save_persistent_devices(self):
        with self.__lock:
            data_to_save = {}
            data_to_save = self.__process_connected_devices(data_to_save)
            data_to_save = self.__process_disconnected_devices(data_to_save)

            with open(self._config_dir + CONNECTED_DEVICES_FILENAME, 'w') as config_file:
                try:
                    config_file.write(dumps(data_to_save, indent=2, sort_keys=True))
                except Exception as e:
                    log.error("Error while saving connected devices to file with error: %s", e, exc_info=e)

            log.debug("Saved connected devices.")

    def __check_devices_idle_time(self):
        check_devices_idle_every_sec = self.__devices_idle_checker.get('inactivityCheckPeriodSeconds', 1)
        disconnect_device_after_idle = self.__devices_idle_checker.get('inactivityTimeoutSeconds', 50)

        while not self.stopped:
            for_deleting = []
            for (device_name, device) in self.__connected_devices.items():
                ts = time()

                if not device.get('last_receiving_data'):
                    device['last_receiving_data'] = ts

                last_receiving_data = device['last_receiving_data']

                if ts - last_receiving_data >= disconnect_device_after_idle:
                    for_deleting.append(device_name)

            for device_name in for_deleting:
                self.del_device(device_name)

                log.debug('Delete device %s for the reason of idle time > %s.',
                          device_name,
                          disconnect_device_after_idle)

            self.stop_event.wait(check_devices_idle_every_sec)

    @CountMessage('msgsSentToPlatform')
    def send_telemetry(self, telemetry, quality_of_service=None, wait_for_publish=True):
        return self.tb_client.client.send_telemetry(telemetry, quality_of_service=quality_of_service,
                                                    wait_for_publish=wait_for_publish)

    @CountMessage('msgsSentToPlatform')
    def gw_send_telemetry(self, device, telemetry, quality_of_service=1):
        return self.tb_client.client.gw_send_telemetry(device, telemetry, quality_of_service=quality_of_service)

    @CountMessage('msgsSentToPlatform')
    def send_attributes(self, attributes, quality_of_service=None, wait_for_publish=True):
        return self.tb_client.client.send_attributes(attributes, quality_of_service=quality_of_service,
                                                     wait_for_publish=wait_for_publish)

    @CountMessage('msgsSentToPlatform')
    def gw_send_attributes(self, device, attributes, quality_of_service=1):
        return self.tb_client.client.gw_send_attributes(device, attributes, quality_of_service=quality_of_service)

    # GETTERS --------------------
    def ping(self):
        return self.name

    def get_max_payload_size_bytes(self):
        if hasattr(self.tb_client.client, 'max_payload_size'):
            return self.tb_client.get_max_payload_size()
        if hasattr(self, '_TBGatewayService__max_payload_size_in_bytes'):
            return int(self.__max_payload_size_in_bytes * 0.9)

        return 8196

    def get_converted_data_queue(self):
        return self.__converted_data_queue

    # ----------------------------
    # Storage --------------------
    def get_storage_name(self):
        return self._event_storage.__class__.__name__

    def get_storage_events_count(self):
        return self._event_storage.len()

    # Connectors -----------------
    def get_available_connectors(self):
        return {num + 1: name for (num, name) in enumerate(self.available_connectors_by_name)}

    def get_connector_status(self, name):
        try:
            connector = self.available_connectors_by_name[name]
            return {'connected': connector.is_connected()}
        except KeyError:
            return f'Connector {name} not found!'

    def get_connector_config(self, name):
        try:
            connector = self.available_connectors_by_name[name]
            return connector.get_config()
        except KeyError:
            return f'Connector {name} not found!'

    # Gateway ----------------------
    def get_status(self):
        return {'connected': self.tb_client.is_connected()}

    def update_loggers(self):
        self.__update_base_loggers()
        TbLogger.update_file_handlers()

        global log
        log = logging.getLogger('service')
        self._event_storage.update_logger()
        self.tb_client.update_logger()

    def __update_base_loggers(self):
        for logger_name in TBRemoteLoggerHandler.LOGGER_NAME_TO_ATTRIBUTE_NAME:
            logger = logging.getLogger(logger_name)

            if self.remote_handler not in logger.handlers:
                if logger.name == 'tb_connection' and logger.level <= 10:
                    continue

                logger.addHandler(self.remote_handler)

    def is_latency_metrics_enabled(self):
        return self.__latency_debug_mode

    # custom rpc method ---------------
    def load_custom_rpc_methods(self, folder_path):
        """
        Dynamically load custom RPC methods from the specified folder.
        """
        if not os.path.exists(folder_path):
            return

        for filename in os.listdir(folder_path):
            if filename.endswith(".py"):
                module_name = filename[:-3]
                module_path = os.path.join(folder_path, filename)
                self.import_custom_rpc_methods(module_name, module_path)

    def import_custom_rpc_methods(self, module_name, module_path):
        """
        Import custom RPC methods from a given Python file.
        """
        spec = spec_from_file_location(module_name, module_path)
        custom_module = module_from_spec(spec)
        spec.loader.exec_module(custom_module)

        # Iterate through the attributes of the module
        for attr_name in dir(custom_module):
            attr = getattr(custom_module, attr_name)
            # Check if the attribute is a function
            if callable(attr):
                # Add the method to the __gateway_rpc_methods dictionary
                self.__gateway_rpc_methods[attr_name.replace("__rpc_", "")] = attr.__get__(self)


if __name__ == '__main__':
    TBGatewayService(
        path.dirname(path.dirname(path.abspath(__file__))) + '/config/tb_gateway.json'.replace('/', path.sep))
