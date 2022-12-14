import asyncio
import asyncer
import binascii
from bleak import BleakClient
import paho.mqtt.client as mqtt
import pyotp
import struct
import json
import asyncio_mqtt 
import configparser

config = configparser.ConfigParser()
config.read('settings.ini')

# Device Settings
address = config["device"]["address"]
interface = config["device"]["interface"]

# Connection Settings
endpoint_name = config["connection"]["endpoint"]
machine_id = config["connection"]["machine_id"]
key = config["connection"]["otpcode"]

DEV_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"

SHOCK_SERV_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
RESPONSE_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
COMMAND_CHAR_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

def unpack_frame(inp):
	if len(inp) < 5:
		return (None, None)

	magic0, magic1, payload_len, type_ = inp[:4]

	if magic0 != 0xb4:
		return (None, None)
	if magic1 != 0x4b:
		return (None, None)

	payload = inp[4:4 + payload_len]
	csum = inp[4 + payload_len:]

	if len(payload) != payload_len:
		return (None, None)

	if len(csum) < 1:
		return (None, None)

	csum = csum[0]
	actual_sum = 0
	for b in inp[:4 + payload_len]:
		actual_sum += b
	if (actual_sum & 0xFF) != csum:
		return (None, None)

	return (type_, payload)

def pack_frame(type_, payload):
	pkt = b'\xb4\x4b' + bytes([len(payload), type_]) + payload

	csum = 0
	for b in pkt:
		csum += b
	pkt += bytes([csum & 0xff])

	return pkt


class ReplyCBWrapper:
	responses = []
	event = asyncio.Event()

	def reply_cb(self, _handle, data):
		# print(f"resp {binascii.hexlify(data)}")
		resp_type, resp_payload = unpack_frame(data)
		self.responses.append([resp_type, resp_payload])
		self.event.set()
		if resp_type is None:
			print(f"INVALID resp {binascii.hexlify(data)}")
		else:
			print(f"resp type {resp_type} payload {binascii.hexlify(resp_payload)}")

	def __await__(self):
		async def fun():
			await self.event.wait()
			out = self.responses.pop()
			if (len(self.responses) == 0):
				self.event.clear()
			return out
		return fun().__await__()

	def clear(self):
		self.responses = []
		self.event.clear()

reply_cb_thing = ReplyCBWrapper()

async def ble_connect(address, device=None):
	# async with BleakClient(address) as client:
	client = BleakClient(address, device=device)
	await client.connect()

	dev_name = await client.read_gatt_char(DEV_NAME_UUID)
	print("Model Number: {0}".format("".join(map(chr, dev_name))))

	services = await client.get_services()
	shock_service = services.get_service(SHOCK_SERV_UUID)
	print(shock_service)

	response_characteristic = shock_service.get_characteristic(RESPONSE_CHAR_UUID)
	command_characteristic = shock_service.get_characteristic(COMMAND_CHAR_UUID)
	print(response_characteristic, command_characteristic)

	# await client.start_notify(response_characteristic, reply_cb)

	return (client, response_characteristic, command_characteristic)


totp = pyotp.TOTP(key)

lastKey = None

def create_config_pkt(voice, volume, 
		vibration, shock, 
		idx=1, num=1):
	frmt = ">BHBHBHBBB"
	b1 = struct.pack(frmt, idx, num, 0x00, 0x01, 0x3c, voice, volume, vibration, shock)
	csum = 0
	for b in b1:
		csum += b
	b1 += bytes([csum & 0xff])
	return pack_frame(0x27, b1)

async def doOutput(message):
	global lastKey

	if (message["value"] != lastKey):
		lastKey = message["value"]
		try:
			if ((not "mode" in message) or (message["mode"] == "config_run")):
				reply_cb_thing.clear()
				await ble_client.start_notify(response_characteristic, reply_cb_thing.reply_cb)
				await ble_client.write_gatt_char(command_characteristic, pack_frame(20, bytes([])), False)
				await asyncio.wait_for(reply_cb_thing, timeout=5)
				reply_cb_thing.clear()
				voice = message["voice"] if "voice" in message else 0
				volume = message["vol"] if "vol" in message else 0
				vibration = message["vibration"] if "vibration" in message else 0
				shock = message["shock"] if "shock" in message else 50
				await ble_client.write_gatt_char(command_characteristic, create_config_pkt(voice, volume, vibration, shock), False)
				await asyncio.wait_for(reply_cb_thing, timeout=5)
				reply_cb_thing.clear()
				await ble_client.write_gatt_char(command_characteristic, pack_frame(0x2A, bytes([])), False)
				await asyncio.wait_for(reply_cb_thing, timeout=5)
				reply_cb_thing.clear()
			elif (message["mode"] == "shock"):
				reply_cb_thing.clear()
				await ble_client.start_notify(response_characteristic, reply_cb_thing.reply_cb)
				power = message["shock"] if "shock" in message else 50
				await ble_client.write_gatt_char(command_characteristic, pack_frame(16, bytes([power])), False)
			elif (message["mode"] == "vibration"):
				reply_cb_thing.clear()
				await ble_client.start_notify(response_characteristic, reply_cb_thing.reply_cb)
				power = message["vibration"] if "vibration" in message else 3
				await ble_client.write_gatt_char(command_characteristic, pack_frame(0x0F, bytes([power])), False)
		except Exception as e:
			print(e)

async def main():
	global ble_client, response_characteristic, command_characteristic
	ble_client, response_characteristic, command_characteristic = await ble_connect(address, interface)
	print(ble_client)
	async with asyncio_mqtt.Client("broker.hivemq.com", 1883, client_id=machine_id, clean_session=False) as mqttc:
		async with mqttc.filtered_messages(endpoint_name) as messages:
			await mqttc.subscribe(endpoint_name, qos=1)
			async for msg in messages:
				try:
					message = json.loads(msg.payload.decode())
					if totp.verify(message["value"]):
						await doOutput(message)
				except json.JSONDecodeError:
					pass


asyncio.run(main())