import asyncio
import logging
import re
from functools import partial, partialmethod
from hashlib import sha1
from importlib import import_module

from hbmqtt.client import ClientException, MQTTClient
from hbmqtt.mqtt.constants import QOS_1

from .config import validate_and_normalise_config
from .modules import BASE_SCHEMA as MODULE_BASE_SCHEMA
from .modules import install_missing_requirements
from .modules.gpio import PinDirection, PinPUD

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.StreamHandler())
_LOG.setLevel(logging.DEBUG)

SET_TOPIC = "set"
SET_ON_MS_TOPIC = "set_on_ms"
SET_OFF_MS_TOPIC = "set_off_ms"
OUTPUT_TOPIC = "output"

MODULE_CLASS_NAMES = dict(gpio="GPIO", sensor="Sensor")


def _init_gpio_module(module_config, module_type):
    module = import_module(
        "mqtt_gpio.modules.%s.%s" % (module_type, module_config["module"])
    )
    # Doesn't need to be a deep copy because we're not mutating the base rules
    module_schema = MODULE_BASE_SCHEMA.copy()
    # Add the module's config schema to the base schema
    module_schema.update(getattr(module, "CONFIG_SCHEMA", {}))
    module_config = validate_and_normalise_config(module_config, module_schema)
    install_missing_requirements(module)
    return getattr(module, MODULE_CLASS_NAMES[module_type])(module_config)


class MqttGpio:
    def __init__(self, config):
        self.config = config
        self.gpio_configs = {}
        self.digital_input_configs = {}
        self.digital_output_configs = {}
        self.gpio_modules = {}
        self.module_output_queues = {}

        self.loop = asyncio.get_event_loop()
        self.unawaited_tasks = []

        self._init_gpio_modules()
        self._init_digital_inputs()
        self._init_digital_outputs()

    def _init_gpio_modules(self):
        self.gpio_configs = {x["name"]: x for x in self.config["gpio_modules"]}
        self.gpio_modules = {}
        for gpio_config in self.config["gpio_modules"]:
            self.gpio_modules[gpio_config["name"]] = _init_gpio_module(
                gpio_config, "gpio"
            )

    def _init_digital_inputs(self):
        self.digital_input_configs = {x["name"]: x for x in self.config["digital_inputs"]}
        # IDEA: Possible implementations -@flyte at 07/04/2019, 15:34:39
        # Could this be done asynchronously?
        for in_conf in self.config["digital_inputs"]:
            pud = None
            if in_conf["pullup"]:
                pud = PinPUD.UP
            elif in_conf["pulldown"]:
                pud = PinPUD.DOWN
            module = self.gpio_modules[in_conf["module"]]
            module.setup_pin(in_conf["pin"], PinDirection.INPUT, pud, in_conf)

            async def input_loop():
                last_value = None
                while True:
                    value = await module.async_get_pin(in_conf["pin"])
                    if value != last_value:
                        await self.mqtt.publish(
                            "%s/input/%s"
                            % (self.config["topic_prefix"], in_conf["name"]),
                            in_conf["on_payload"] if value else in_conf["off_payload"],
                        )
                    last_value = value
                    await asyncio.sleep(0.1)

            self.unawaited_tasks.append(self.loop.create_task(input_loop()))

    def _init_digital_outputs(self):
        self.digital_output_configs = {
            x["name"]: x for x in self.config["digital_outputs"]
        }
        # IDEA: Possible implementations -@flyte at 07/04/2019, 15:34:54
        # Could this be done asynchronously?
        for out_conf in self.config["digital_outputs"]:
            self.gpio_modules[out_conf["module"]].setup_pin(
                out_conf["pin"], PinDirection.OUTPUT, None, out_conf
            )
            # Create queues for each module with an output
            if out_conf["module"] not in self.module_output_queues:
                queue = asyncio.Queue()
                self.module_output_queues[out_conf["module"]] = queue

                # Create loops which handle new entries on the queue
                async def output_loop():
                    while True:
                        await self.set_output(*await queue.get())

                self.unawaited_tasks.append(self.loop.create_task(output_loop()))

    async def set_output(self, module, output_config, payload):
        pin = output_config["pin"]
        if payload == output_config["on_payload"]:
            await module.async_set_pin(pin, True)
        elif payload == output_config["off_payload"]:
            await module.async_set_pin(pin, False)
        else:
            _LOG.warning(
                "Payload received did not match 'on' or 'off' payloads: %r", payload
            )

    async def _init_mqtt(self):
        config = self.config["mqtt"]
        topic_prefix = config["topic_prefix"]

        client_id = config["client_id"]
        if not client_id:
            client_id = "mqtt-gpio-%s" % sha1(topic_prefix.encode("utf8")).hexdigest()

        tls_enabled = config.get("tls", {}).get("enabled")

        uri = "mqtt%s://" % ("s" if tls_enabled else "")
        if config["user"] and config["password"]:
            uri += "%s:%s@" % (config["user"], config["password"])
        uri += "%s:%s" % (config["host"], config["port"])

        client_config = {}
        connect_kwargs = dict(cleansession=False)
        if tls_enabled:
            tls_config = config["tls"]
            if tls_config.get("certfile") and tls_config.get("keyfile"):
                client_config.update(
                    dict(
                        certfile=tls_config.get("certfile") or None,
                        keyfile=tls_config.get("keyfile") or None,
                    )
                )
            client_config["check_hostname"] = not tls_config["insecure"]
            connect_kwargs.update(
                dict(
                    capath=tls_config.get("ca_certs"),
                    cafile=tls_config.get("ca_file"),
                    cadata=tls_config.get("ca_data"),
                )
            )

        self.mqtt = MQTTClient(client_id=client_id, config=client_config, loop=self.loop)
        await self.mqtt.connect(uri, **connect_kwargs)
        await self.mqtt.subscribe([("%s/#" % topic_prefix, QOS_1)])

    async def _mqtt_rx_loop(self):
        try:
            while True:
                msg = await self.mqtt.deliver_message()
                topic = msg.publish_packet.variable_header.topic_name
                payload = msg.publish_packet.payload.data.decode("utf8")
                _LOG.info("Received message on topic %r: %r", topic, payload)
                self._handle_mqtt_msg(topic, payload)
        finally:
            print("\nDisconnecting from MQTT...")
            await self.mqtt.disconnect()
            print("MQTT disconnected")

    def run(self):
        self.loop.run_until_complete(self._init_mqtt())

        async def remove_finished_tasks():
            while True:
                await asyncio.sleep(1)
                finished_tasks = [x for x in self.unawaited_tasks if x.done()]
                if not finished_tasks:
                    continue
                for task in finished_tasks:
                    try:
                        await task
                    except Exception as e:
                        _LOG.exception("Exception in task: %r:", task)
                self.unawaited_tasks = list(
                    filter(lambda x: not x.done(), self.unawaited_tasks)
                )

        # IDEA: Possible implementations -@flyte at 26/05/2019, 16:04:46
        # This is where we add any other async tasks that we want to run, such as polling
        # inputs, sensor loops etc.
        tasks = asyncio.gather(self._mqtt_rx_loop(), remove_finished_tasks())
        try:
            self.loop.run_until_complete(tasks)
        except KeyboardInterrupt:
            tasks.cancel()
            for task in list(self.mqtt.client_tasks) + self.unawaited_tasks:
                task.cancel()
            self.loop.run_until_complete(
                asyncio.gather(
                    *(self.unawaited_tasks + [tasks] + list(self.mqtt.client_tasks)),
                    return_exceptions=True,
                )
            )

    def _poll_inputs(self):
        pass

    def _handle_mqtt_msg(self, topic, payload):
        topic_prefix = self.config["mqtt"]["topic_prefix"]

        if any(
            topic.endswith("/%s" % x)
            for x in (SET_TOPIC, SET_ON_MS_TOPIC, SET_OFF_MS_TOPIC)
        ):
            try:
                output_name = output_name_from_topic(topic, topic_prefix)
            except ValueError as e:
                _LOG.warning("Unable to parse topic: %s", e)
                return
            output_config = self.digital_output_configs[output_name]
            module = self.gpio_modules[output_config["module"]]
            if topic.endswith("/%s" % SET_TOPIC):
                # This is a message to set a digital output to a given value
                self.module_output_queues[output_config["module"]].put_nowait(
                    (module, output_config, payload)
                )
            else:
                value = topic.endswith("/%s" % SET_ON_MS_TOPIC)
                if output_config["inverted"]:
                    value = not value

                async def set_ms():
                    try:
                        secs = float(payload) / 1000
                    except ValueError:
                        _LOG.warning(
                            "Unable to parse ms value as float from payload %r", payload
                        )
                        return
                    await module.async_set_pin(output_config["pin"], value)
                    await asyncio.sleep(secs)
                    await module.async_set_pin(output_config["pin"], not value)

                task = self.loop.create_task(set_ms())
                self.unawaited_tasks.append(task)


def output_name_from_topic(topic, prefix):
    match = re.match("^{}/{}/(.+?)/.+$".format(prefix, OUTPUT_TOPIC), topic)
    if match is None:
        raise ValueError("Topic %r does not adhere to expected structure" % topic)
    return match.group(1)
