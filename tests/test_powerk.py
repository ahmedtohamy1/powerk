import asyncio
import base64
import http.client
import json
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import powerk


MAC = "AABBCCDDEEFF"


class StartupTests(unittest.TestCase):
    def test_default_command_has_all_server_defaults(self):
        serve = AsyncMock(return_value=0)
        with patch.object(sys, "argv", ["powerk.py"]), patch.object(powerk, "cmd_serve", serve):
            self.assertEqual(powerk.main(), 0)
        args = serve.call_args.args[0]
        self.assertEqual(args.token, "")
        self.assertEqual(args.allow_device, [])
        self.assertEqual(args.web_bind, "0.0.0.0")

    def test_allowlist_normalizes_and_rejects_invalid_macs(self):
        self.assertEqual(powerk.device_mac(MAC.lower()), MAC)
        with self.assertRaises(Exception):
            powerk.device_mac("not-a-mac")


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        session = powerk.Session("127.0.0.1", None, None)
        session.device = powerk.Device(MAC, "test", "1", "127.0.0.1")
        session.send = AsyncMock()
        session._refresh = AsyncMock(return_value=True)
        return session

    async def test_status_response_does_not_confirm_unchanged_relay(self):
        session = self.session()
        with patch.object(powerk.asyncio, "sleep", new=AsyncMock()):
            self.assertFalse(await session.set_outlets([1], True))
            session.device.outlets[1]["on"] = True
            self.assertTrue(await session.set_outlets([1], True))
            self.assertFalse(await session.set_outlets([1, 2], True))

    async def test_expired_queued_command_is_never_sent(self):
        hub = powerk.Hub()
        session = self.session()
        session.device.online = True
        session.writer = type("Writer", (), {"is_closing": lambda self: False})()
        hub.sessions[MAC] = session
        await session.lock.acquire()
        with patch.object(powerk, "COMMAND_TIMEOUT", 0.02):
            with self.assertRaises(asyncio.TimeoutError):
                await hub.command(MAC, [1], True)
        session.lock.release()
        await asyncio.sleep(0)
        session.send.assert_not_called()

    async def test_snapshot_is_detached_from_mutable_outlet_state(self):
        session = self.session()
        snapshot = session.device.snapshot()
        session.device.outlets[1]["on"] = True
        self.assertFalse(snapshot["outlets"][0]["on"])


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = powerk.Hub({MAC})
        self.server = await asyncio.start_server(self.hub.handle_client, "127.0.0.1", 0, limit=8192)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []

    async def asyncTearDown(self):
        for _, writer in self.clients:
            writer.close()
            await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        for _ in range(100):
            if self.hub.connections == 0:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.hub.connections, 0)

    async def connect(self, mac=MAC):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.clients.append((reader, writer))
        writer.write(f"up:bootinfo:test;{mac};001122334455;1;connect\r\n".encode())
        await writer.drain()
        return reader, writer

    async def wait_online(self):
        for _ in range(100):
            if self.hub.devices[MAC].online:
                return
            await asyncio.sleep(0.005)
        self.fail("device did not become online")

    async def test_allowlist_rejects_unknown_device(self):
        reader, _ = await self.connect("112233445566")
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(self.hub.devices)

    async def test_duplicate_connection_cannot_replace_live_session(self):
        reader, writer = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.readline(), 1), b"up:getinfo:all\r\n")
        original = self.hub.sessions[MAC]
        second, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(second.read(), 1), b"")
        self.assertIs(self.hub.sessions[MAC], original)
        writer.write(f"up:getinfo:{powerk._fake_tags({1})}\r\n".encode())
        await writer.drain()
        await self.wait_online()
        self.assertIsNotNone(self.hub.devices[MAC].last_seen)

    async def test_duplicate_channels_do_not_confirm_snapshot(self):
        reader, writer = await self.connect()
        await asyncio.wait_for(reader.readline(), 1)
        payload = powerk._fake_tags(set()).replace(":4:", ":3:")
        writer.write(f"up:getinfo:{payload}\r\n".encode())
        await writer.drain()
        await asyncio.sleep(0.03)
        self.assertFalse(self.hub.devices[MAC].online)

    async def test_timeout_marks_offline_and_allows_reconnect(self):
        reader, writer = await self.connect()
        await asyncio.wait_for(reader.readline(), 1)
        writer.write(f"up:getinfo:{powerk._fake_tags(set())}\r\n".encode())
        await writer.drain()
        await self.wait_online()
        session = self.hub.sessions[MAC]
        session.refresh = AsyncMock(return_value=False)
        await self.hub.refresh_session(session)
        self.assertFalse(session.device.online)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        new_reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(new_reader.readline(), 1), b"up:getinfo:all\r\n")


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.history = powerk.History(Path(self.folder.name) / "history.sqlite3")
        self.history.start()
        self.hub = powerk.Hub(history=self.history)
        handler = type("TestWeb", (powerk.Web,), {
            "hub": self.hub, "loop": asyncio.get_running_loop(),
            "token": "secret", "local_ip": "127.0.0.1", "port": 10086,
        })
        self.server = powerk.BoundedHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self):
        await asyncio.get_running_loop().run_in_executor(None, self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=1)
        await asyncio.get_running_loop().run_in_executor(None, self.history.close)
        self.folder.cleanup()

    async def request(self, path="/api/onoff", body=None, token="secret", headers=None):
        def send():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                request_headers = {"X-Token": token, **(headers or {})}
                connection.request("GET" if body is None else "POST", path,
                                   body=body, headers=request_headers)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        return await asyncio.get_running_loop().run_in_executor(None, send)

    async def test_authentication_methods_and_denial(self):
        self.assertEqual((await self.request("/api/state", token="wrong"))[0], 401)
        self.assertEqual((await self.request("/api/state"))[0], 200)
        basic = base64.b64encode(b"powerk:secret").decode()
        self.assertEqual((await self.request("/api/state", token="", headers={"Authorization": "Basic " + basic}))[0], 200)
        self.assertEqual((await self.request("/api/state?t=secret", token=""))[0], 200)

    async def test_routes_are_exact(self):
        self.assertEqual((await self.request("/api/state/extra"))[0], 404)
        self.assertEqual((await self.request("/api/onoff/extra", "{}"))[0], 404)

    async def test_history_and_csv_use_the_existing_authentication(self):
        for path in ("/api/history", "/api/history.csv"):
            self.assertEqual((await self.request(path, token="wrong"))[0], 401)
            status, body = await self.request(path)
            self.assertEqual(status, 200)
            if path.endswith(".csv"):
                self.assertTrue(body.startswith(b"timestamp_utc,unix_seconds,mac,outlet"))
            else:
                self.assertEqual(json.loads(body)["summary"]["readings"], 0)
        self.assertEqual((await self.request("/api/history?outlet=0"))[0], 400)
        self.assertEqual((await self.request("/api/history?start=nan"))[0], 400)

    async def test_history_is_unavailable_when_no_recorder_is_configured(self):
        self.hub.history = None
        self.assertEqual((await self.request("/api/history"))[0], 503)

    async def test_history_api_and_csv_return_the_same_persisted_period(self):
        device = powerk.Device("AABBCCDDEEFF", "test", "1", "127.0.0.1")
        device.online = True
        for at, energy in ((1000, 5), (1030, 5.001)):
            device.last_seen = at
            for outlet in device.outlets.values():
                outlet["energy_kwh"] = energy
            self.history.record(device.snapshot())
        status, body = await self.request("/api/history?start=900&end=2000&outlet=4")
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["summary"]["readings"], 2)
        self.assertEqual(report["summary"]["energy_kwh"], .001)
        status, body = await self.request("/api/history.csv?start=900&end=2000&outlet=4")
        self.assertEqual(status, 200)
        self.assertEqual(len(body.decode().splitlines()), 3)

    async def test_strict_input_validation(self):
        valid = {"mac": MAC, "outlet": 1, "on": False}
        cases = [[], {}, {**valid, "on": "false"}, {**valid, "on": 0},
                 {**valid, "outlet": True}, {**valid, "outlet": 1.5},
                 {**valid, "outlet": "1"}, {**valid, "outlet": 5},
                 {**valid, "mac": "invalid"}]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual((await self.request(body=json.dumps(body)))[0], 400)
        self.assertEqual((await self.request(body="{"))[0], 400)
        self.assertEqual((await self.request(body="x" * (powerk.MAX_BODY + 1)))[0], 400)

    async def test_offline_command_returns_503(self):
        self.assertEqual((await self.request(body=json.dumps({"mac": MAC, "outlet": 1, "on": True})))[0], 503)

    async def test_unconfirmed_command_and_deadline_are_errors(self):
        body = json.dumps({"mac": MAC, "outlet": 1, "on": True})
        self.hub.command = AsyncMock(return_value=False)
        status, response = await self.request(body=body)
        self.assertEqual(status, 502)
        self.assertFalse(json.loads(response)["confirmed"])
        self.hub.command = AsyncMock(side_effect=asyncio.TimeoutError())
        self.assertEqual((await self.request(body=body))[0], 504)

    async def test_http_to_strip_round_trip_and_rejected_relay(self):
        tcp = await asyncio.start_server(self.hub.handle_client, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp.sockets[0].getsockname()[1])
        state_on = set()
        accept_commands = True

        async def fake_strip():
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode().strip()
                if line == "up:getinfo:all":
                    writer.write(f"up:getinfo:{powerk._fake_tags(state_on)}\r\n".encode())
                    await writer.drain()
                elif line.startswith("up:onoff:") and accept_commands:
                    _, _, channel, value = line.split(":")
                    (state_on.add if value == "on" else state_on.discard)(int(channel))

        task = asyncio.create_task(fake_strip())
        try:
            writer.write(f"up:bootinfo:test;{MAC};001122334455;1;connect\r\n".encode())
            await writer.drain()
            for _ in range(100):
                if self.hub.session(MAC):
                    break
                await asyncio.sleep(0.005)
            self.assertIsNotNone(self.hub.session(MAC))
            body = json.dumps({"mac": MAC, "outlet": 0, "on": True})
            status, response = await self.request(body=body)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(response)["confirmed"])
            self.assertEqual(state_on, {1, 2, 3, 4})
            accept_commands = False
            body = json.dumps({"mac": MAC, "outlet": 2, "on": False})
            status, response = await self.request(body=body)
            self.assertEqual(status, 502)
            self.assertFalse(json.loads(response)["confirmed"])
            self.assertIn(2, state_on)
            await asyncio.get_running_loop().run_in_executor(None, self.history.pending.join)
            self.assertEqual(self.history.status()["readings_saved"], 4)
            self.assertIsNone(self.history.status()["error"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            tcp.close()
            await tcp.wait_closed()


if __name__ == "__main__":
    unittest.main()
