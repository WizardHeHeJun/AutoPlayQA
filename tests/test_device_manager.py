from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from core.device_manager import DeviceManager
from user_interface.command_parser import parse_command


def completed(args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def manager(fake_logger):
    return DeviceManager(fake_logger)


# ---------- discover ----------

def test_discover_marks_wireless_devices(manager):
    out = (
        "List of devices attached\n"
        "ABC123 device product:example model:EXAMPLE_MODEL\n"
        "192.168.1.100:5555 device model:EXAMPLE_MODEL\n"
        "emulator-5554 device model:sdk_gphone\n"
    )
    with patch("core.device_manager.subprocess.run", return_value=completed([], stdout=out)):
        devices = manager.discover_devices()
    types = {d.device_id: d.device_type for d in devices}
    assert types == {
        "ABC123": "physical",
        "192.168.1.100:5555": "wireless",
        "emulator-5554": "emulator",
    }


# ---------- connect / disconnect ----------

def test_connect_appends_default_port_and_succeeds(manager):
    with patch(
        "core.device_manager.subprocess.run",
        return_value=completed([], stdout="connected to 192.168.1.100:5555"),
    ) as run:
        result = manager.connect("192.168.1.100")
    assert result["ok"] is True
    assert result["address"] == "192.168.1.100:5555"
    assert run.call_args[0][0] == ["adb", "connect", "192.168.1.100:5555"]


def test_connect_already_connected_counts_as_success(manager):
    with patch(
        "core.device_manager.subprocess.run",
        return_value=completed([], stdout="already connected to 192.168.1.100:5555"),
    ):
        assert manager.connect("192.168.1.100:5555")["ok"] is True


def test_connect_failure_despite_zero_exit(manager):
    # adb connect exits 0 even when the target is unreachable.
    with patch(
        "core.device_manager.subprocess.run",
        return_value=completed([], stdout="cannot connect to 192.168.1.100:5555: (10061)"),
    ):
        result = manager.connect("192.168.1.100")
    assert result["ok"] is False
    assert "cannot connect" in result["message"]


def test_connect_without_adb(manager):
    with patch("core.device_manager.subprocess.run", side_effect=FileNotFoundError):
        assert manager.connect("192.168.1.100")["ok"] is False


def test_disconnect_all_when_no_address(manager):
    with patch(
        "core.device_manager.subprocess.run",
        return_value=completed([], stdout="disconnected everything"),
    ) as run:
        result = manager.disconnect()
    assert result["ok"] is True
    assert run.call_args[0][0] == ["adb", "disconnect"]


# ---------- pair ----------

def test_pair_success(manager):
    with patch(
        "core.device_manager.subprocess.run",
        return_value=completed([], stdout="Successfully paired to 192.168.1.100:37123 [guid=adb-x]"),
    ):
        assert manager.pair("192.168.1.100:37123", "123456")["ok"] is True


def test_pair_failure(manager):
    with patch(
        "core.device_manager.subprocess.run",
        return_value=completed([], stdout="Failed: Wrong password or connection was dropped.", returncode=1),
    ):
        assert manager.pair("192.168.1.100:37123", "000000")["ok"] is False


# ---------- enable_tcpip ----------

def test_enable_tcpip_returns_connect_address(manager):
    def fake_run(cmd, **kwargs):
        if "route" in cmd:
            return completed(
                cmd,
                stdout="192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.100\n",
            )
        return completed(cmd, stdout="restarting in TCP mode port: 5555")

    with patch("core.device_manager.subprocess.run", side_effect=fake_run):
        result = manager.enable_tcpip("ABC123")
    assert result["ok"] is True
    assert result["address"] == "192.168.1.100:5555"


def test_enable_tcpip_without_wifi_ip_still_ok(manager):
    def fake_run(cmd, **kwargs):
        if "route" in cmd:
            return completed(cmd, stdout="")
        return completed(cmd, stdout="restarting in TCP mode port: 5555")

    with patch("core.device_manager.subprocess.run", side_effect=fake_run):
        result = manager.enable_tcpip("ABC123")
    assert result["ok"] is True
    assert result["address"] is None
    assert "IP not found" in result["message"]


def test_enable_tcpip_failure(manager):
    def fake_run(cmd, **kwargs):
        if "route" in cmd:
            return completed(cmd, stdout="")
        return completed(cmd, stderr="error: device 'ABC123' not found", returncode=1)

    with patch("core.device_manager.subprocess.run", side_effect=fake_run):
        assert manager.enable_tcpip("ABC123")["ok"] is False


# ---------- CLI parsing ----------

def test_parse_device_connect():
    assert parse_command("device connect 192.168.1.100") == {
        "type": "device_connect",
        "address": "192.168.1.100",
    }


def test_parse_device_disconnect_without_address():
    assert parse_command("device disconnect") == {"type": "device_disconnect", "address": None}


def test_parse_device_tcpip_with_port():
    assert parse_command("device tcpip ABC123 6666") == {
        "type": "device_tcpip",
        "device_id": "ABC123",
        "port": 6666,
    }


def test_parse_device_tcpip_default_port():
    assert parse_command("device tcpip ABC123")["port"] == 5555


def test_parse_device_pair():
    assert parse_command("device pair 192.168.1.100:37123 123456") == {
        "type": "device_pair",
        "address": "192.168.1.100:37123",
        "code": "123456",
    }
