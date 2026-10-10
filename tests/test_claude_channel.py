"""Claude channel 传输契约、窗口隔离与错误边界；不调用真实模型。"""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server import MCPServer
from mcp.server.session import ServerSession

from harness_mcp.claude_channel import (
    CHANNEL_METHOD, ChannelDeliveryError, ClaudeChannel, configure_channel,
)


class ClaudeCapabilityTests(unittest.TestCase):
    def test_capability_preserves_existing_sdk_options_without_permission_relay(self):
        server = MCPServer("fixture", instructions="existing instructions", version="fixture")
        configure_channel(server)
        options = server._lowlevel_server.create_initialization_options(experimental_capabilities={"other": {"enabled": True}})
        self.assertEqual(options.capabilities.experimental, {"other": {"enabled": True}, "claude/channel": {}})
        self.assertEqual(options.instructions, "existing instructions")
        self.assertNotIn("claude/channel/permission", options.capabilities.experimental)

    def test_disabled_does_not_change_initialize_response(self):
        server = MCPServer("fixture")
        configure_channel(server, enabled=False)
        self.assertIsNone(server._lowlevel_server.create_initialization_options().capabilities.experimental)

    def test_configure_is_idempotent_and_accepts_positional_sdk_arguments(self):
        server = MCPServer("fixture")
        configure_channel(server)
        configured = server._lowlevel_server.create_initialization_options
        configure_channel(server)
        self.assertIs(server._lowlevel_server.create_initialization_options, configured)
        options = configured(None, {"other": {}})
        self.assertEqual(options.capabilities.experimental, {"other": {}, "claude/channel": {}})

    def test_explicit_flag_requires_boolean(self):
        with self.assertRaises(ValueError):
            configure_channel(MCPServer("fixture"), enabled="true")


class ClaudeChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = ClaudeChannel()
        self.session = SimpleNamespace(protocol_version="2025-11-25", send_notification=AsyncMock())

    async def test_notification_uses_official_method_and_plain_meta(self):
        origin = self.channel.attach_context(SimpleNamespace(session=self.session))
        receipt = await self.channel.send_completion(origin, "固定的完成通知，不包含模型报告。", "harness-completion-fixture-1")
        notification = self.session.send_notification.await_args.args[0]
        wire = notification.model_dump(by_alias=True, mode="json", exclude_none=True)
        self.assertEqual(wire, {"method": CHANNEL_METHOD, "params": {"content": "固定的完成通知，不包含模型报告。", "meta": {"delivery_id": "harness-completion-fixture-1"}}})
        self.assertEqual(receipt, {"status": "channel_written", "acknowledged": False})
        self.assertEqual(self.session.send_notification.await_args.kwargs, {})

    async def test_distinct_frontends_cannot_send_to_each_other(self):
        other = ClaudeChannel()
        self.channel.attach_context(SimpleNamespace(session=self.session))
        with self.assertRaises(ChannelDeliveryError) as raised:
            await self.channel.send_completion(other.origin(), "notification", "fixture-2")
        self.assertEqual(raised.exception.code, "COMPLETION_TARGET_MISMATCH")
        self.assertFalse(raised.exception.uncertain)
        self.session.send_notification.assert_not_awaited()

    async def test_origin_has_fixed_private_canonical_uuids(self):
        origin = self.channel.origin()
        self.assertEqual(set(origin), {"client", "connection_id", "session_id"})
        self.assertEqual(origin["client"], "claude_code")
        self.assertEqual(len(origin["connection_id"]), 36)
        self.assertNotEqual(origin["connection_id"], origin["session_id"])
        origin["session_id"] = "tampered"
        self.assertNotEqual(self.channel.origin(), origin)

    async def test_missing_context_fails_before_write(self):
        with self.assertRaises(ChannelDeliveryError) as raised:
            self.channel.attach_context(None)
        self.assertFalse(raised.exception.uncertain)
        with self.assertRaises(ChannelDeliveryError) as raised:
            await self.channel.send_completion(self.channel.origin(), "notification", "fixture-3")
        self.assertFalse(raised.exception.uncertain)

    async def test_modern_or_unknown_protocol_is_not_claimed_supported(self):
        for protocol in ("2026-07-28", "2099-01-01", None):
            session = SimpleNamespace(protocol_version=protocol, send_notification=AsyncMock())
            with self.assertRaises(ChannelDeliveryError) as raised:
                self.channel.attach_context(SimpleNamespace(session=session))
            self.assertEqual(raised.exception.code, "CLAUDE_CHANNEL_PROTOCOL_UNAVAILABLE")
            self.assertFalse(raised.exception.uncertain)
            session.send_notification.assert_not_awaited()

    async def test_transport_error_does_not_imply_undelivered(self):
        self.channel.attach_context(SimpleNamespace(session=self.session))
        self.session.send_notification.side_effect = OSError("private error content")
        with self.assertRaises(ChannelDeliveryError) as raised:
            await self.channel.send_completion(self.channel.origin(), "notification", "fixture-4")
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn("private", str(raised.exception))

    async def test_real_sdk_session_sends_on_standalone_channel_after_request_ends(self):
        request = SimpleNamespace(notify=AsyncMock(side_effect=OSError("request already closed")))
        outbound = SimpleNamespace(notify=AsyncMock())
        connection = SimpleNamespace(protocol_version="2025-11-25", outbound=outbound)
        session = ServerSession(request, connection)
        self.channel.attach_context(SimpleNamespace(session=session))
        receipt = await self.channel.send_completion(self.channel.origin(), "completion notice", "fixture-sdk")
        outbound.notify.assert_awaited_once_with(CHANNEL_METHOD, {"content": "completion notice", "meta": {"delivery_id": "fixture-sdk"}})
        request.notify.assert_not_awaited()
        self.assertEqual(receipt["status"], "channel_written")

    async def test_invalid_reconnect_and_detach_do_not_retain_old_session(self):
        self.channel.attach_context(SimpleNamespace(session=self.session))
        with self.assertRaises(ChannelDeliveryError):
            self.channel.attach_context(None)
        with self.assertRaises(ChannelDeliveryError) as raised:
            await self.channel.send_completion(self.channel.origin(), "notification", "fixture-stale")
        self.assertFalse(raised.exception.uncertain)
        self.channel.attach_context(SimpleNamespace(session=self.session))
        self.channel.detach()
        with self.assertRaises(ChannelDeliveryError) as raised:
            await self.channel.send_completion(self.channel.origin(), "notification", "fixture-detached")
        self.assertFalse(raised.exception.uncertain)
        self.session.send_notification.assert_not_awaited()

    async def test_invalid_payload_and_target_fail_before_write(self):
        self.channel.attach_context(SimpleNamespace(session=self.session))
        for origin, prompt, call_id in ((self.channel.origin(), "", "fixture-5"), (self.channel.origin(), "notification", "x\nunsafe"), ({**self.channel.origin(), "pipe_path": "unrelated"}, "notification", "fixture-5")):
            with self.assertRaises(ChannelDeliveryError) as raised:
                await self.channel.send_completion(origin, prompt, call_id)
            self.assertFalse(raised.exception.uncertain)
        self.session.send_notification.assert_not_awaited()

    async def test_rejects_noncanonical_identity(self):
        for value in ("invalid", "00000000000000000000000000000000", "A0000000-0000-0000-0000-000000000000"):
            with self.assertRaises(ValueError):
                ClaudeChannel(connection_id=value)


if __name__ == "__main__":
    unittest.main()
